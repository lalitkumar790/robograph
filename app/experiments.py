import logging
import time
import uuid
from contextlib import contextmanager

from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeout

from app import assess, core, db, tools
from app.agent import investigate
from app.db import query

log = logging.getLogger("robograph")

CHANGES = {
    "remove_recoveries": (
        "Remove the recovery evidence for moving to {loc}",
        "MATCH (:Incident)-[v:RESOLVED_VIA]->(:Location {id: $loc}) DELETE v",
    ),
    "add_failure": (
        "Add a failed comm_loss incident at {loc}",
        "MATCH (r:Robot {id: 'R1'}), (l:Location {id: $loc}) "
        "CREATE (r)-[:EXPERIENCED]->(:Incident {id: 'INC-9001', type: 'comm_loss', ts: $ts, severity: 3, "
        "summary: 'Injected for the counterfactual.', resolution: 'Link did not recover.', outcome: 'failed'})-[:OCCURRED_AT]->(l)",
    ),
}


@contextmanager
def isolated_copy():
    name = f"{db.GRAPH_NAME}_cf_{uuid.uuid4().hex[:8]}"
    try:
        db.graph().copy(name)
        with db.use_graph(name):
            yield name
    finally:
        try:
            db.graph(name).delete()
        except Exception:
            log.debug("no graph copy %s to delete", name)


def drop_stale_copies():
    for name in db.client().list_graphs():
        if name.startswith(f"{db.GRAPH_NAME}_cf_"):
            db.graph(name).delete()


def _fingerprint():
    return (query("MATCH (n) RETURN labels(n)[0] AS k, count(n) AS c ORDER BY k"),
            query("MATCH ()-[r]->() RETURN type(r) AS k, count(r) AS c ORDER BY k"))


def _apply(change, loc):
    query(CHANGES[change][1], {"loc": loc, "ts": int(time.time())})


def _label(rec):
    return rec["action"].replace("_", " ") + (f" to {rec['target']}" if rec["target"] else "")


def _texts(option, field):
    return {e["text"] for e in option[field]} if option else set()


def _option_changes(before, after):
    old = {(o["action"], o["target"]): o for o in before["options"]}
    new = {(o["action"], o["target"]): o for o in after["options"]}
    changes = []
    for key in sorted(old.keys() | new.keys(), key=lambda k: (k[0], k[1] or "")):
        o, n = old.get(key), new.get(key)
        diff = {"support_removed": sorted(_texts(o, "evidence_for") - _texts(n, "evidence_for")),
                "support_added": sorted(_texts(n, "evidence_for") - _texts(o, "evidence_for")),
                "contradiction_removed": sorted(_texts(o, "evidence_against") - _texts(n, "evidence_against")),
                "contradiction_added": sorted(_texts(n, "evidence_against") - _texts(o, "evidence_against"))}
        verdict_before, verdict_after = o and o["verdict"], n and n["verdict"]
        if verdict_before != verdict_after or any(diff.values()):
            changes.append({"action": key[0], "target": key[1], "before": verdict_before, "after": verdict_after,
                            "verdict_changed": verdict_before != verdict_after,
                            "reason_before": o and o["reason"], "reason_after": n and n["reason"], **diff})
    return changes


def _outcome(before, after):
    if (before["action"], before["target"]) == (after["action"], after["target"]):
        return "unchanged"
    return "uncertain" if after["action"] == "request_review" else "changed"


def _explain(change, description, before, after, changes, outcome):
    head = {"unchanged": "The recommendation did not change.", "changed": "The recommendation changed.",
            "uncertain": "The recommendation became uncertain: nothing is supported any more."}[outcome]
    text = f"Before, the evidence supported: {_label(before['recommended'])}. After '{description}': {_label(after['recommended'])}. {head}"
    for c in changes:
        name = c["target"] or c["action"].replace("_", " ")
        line = f"{name}: {c['before']} to {c['after']}." if c["verdict_changed"] else f"{name}: still {c['after']}, evidence changed."
        for label, key in (("Support removed", "support_removed"), ("Support added", "support_added"),
                           ("Contradiction removed", "contradiction_removed"), ("Contradiction added", "contradiction_added")):
            if c[key]:
                line += f" {label}: {'; '.join(c[key])}."
        text += " " + line
    if change == "remove_recoveries":
        text += " Removing records shows what the recommendation depends on; it does not mean the location would fail."
    return text + " The original graph was not modified."


def _agent_run(client, robot_id, after):
    try:
        run = investigate(client, robot_id, with_assessment=False, fallback=False)
    except (RedisConnectionError, RedisTimeout):
        raise
    except Exception as e:
        log.exception("agent failed on the graph copy")
        return {"completed": False, "decision": None, "agrees_with_evidence": None,
                "reason": f"the agent run failed ({type(e).__name__})", "tool_calls": [], "elapsed_ms": 0}
    d, rec = run["decision"], after["recommended"]
    return {"completed": run["agent_completed"], "reason": run["reason"], "tool_calls": run["tool_calls"],
            "elapsed_ms": run["elapsed_ms"], "timing": run["timing"],
            "decision": {k: d[k] for k in ("action", "target", "summary")} if d else None,
            "agrees_with_evidence": None if not d else (d["action"], d["target"]) == (rec["action"], rec["target"])}


def counterfactual(robot_id, change, location_id, client=None):
    if change not in CHANGES:
        raise core.ToolError(f"change must be one of {', '.join(CHANGES)}")
    core.robot(robot_id)
    core.location(location_id)
    description = CHANGES[change][0].format(loc=location_id)
    fingerprint = _fingerprint()
    baseline = assess.assess_incident(robot_id)
    out = {"change": {"type": change, "location_id": location_id, "description": description}, "baseline": baseline,
           "counterfactual": None, "outcome": "failed", "changed": False, "option_changes": [], "agent": None,
           "simulation": {"ok": True, "error": None}}
    try:
        with isolated_copy():
            _apply(change, location_id)
            out["counterfactual"] = assess.assess_incident(robot_id)
            if client is not None:
                out["agent"] = _agent_run(client, robot_id, out["counterfactual"])
    except (RedisConnectionError, RedisTimeout):
        raise
    except Exception as e:
        log.exception("counterfactual simulation failed")
        out["counterfactual"] = None
        out["simulation"] = {"ok": False, "error": f"{type(e).__name__} while working on the graph copy"}
    out["original_unchanged"] = _fingerprint() == fingerprint
    if out["counterfactual"] is None:
        out["explanation"] = "The simulation failed, so nothing can be concluded about this change. The graph copy was removed and the original graph was not modified."
        return out
    out["option_changes"] = _option_changes(baseline, out["counterfactual"])
    out["outcome"] = _outcome(baseline["recommended"], out["counterfactual"]["recommended"])
    out["changed"] = out["outcome"] != "unchanged"
    out["explanation"] = _explain(change, description, baseline, out["counterfactual"], out["option_changes"], out["outcome"])
    return out


def _create_blank_robot():
    query("CREATE (r:Robot {id: 'R9', name: 'Test robot', model: 'Rover-A'}) WITH r MATCH (l:Location {id: 'W3'}) "
          "CREATE (r)-[:LOCATED_AT]->(l) "
          "CREATE (r)-[:GENERATED]->(:Telemetry {id: 'T-9', ts: $ts, rssi: -90, battery: 80, temp_c: 40})-[:RECORDED_AT]->(l)",
          {"ts": int(time.time())})


SCENARIOS = [
    {"name": "Communication loss with recovery history", "robot": "R4", "expect": ("reroute", "W7"),
     "why": "R1 and R2 recovered from W5 by moving to W7, and W7 has strong signal and no failures."},
    {"name": "Motor overheating", "robot": "R3", "expect": ("hold_and_service", None),
     "why": "Overheating is a component fault. Earlier motor incidents recovered after servicing, so rerouting is not offered."},
    {"name": "Healthy robot", "robot": "R1", "expect": ("continue_monitoring", None),
     "why": "No telemetry threshold is exceeded."},
    {"name": "Missing evidence", "robot": "R9", "setup": _create_blank_robot, "expect": ("request_review", None),
     "why": "A new robot with a weak signal and no incident history: no option has recovery evidence, so the answer is to ask for review."},
    {"name": "Conflicting evidence", "robot": "R4", "setup": lambda: _apply("add_failure", "W7"), "expect": ("request_review", None),
     "why": "W7 has two recoveries and now also a failed incident. Both sides are shown and nothing is recommended."},
    {"name": "Counterfactual: recovery evidence removed", "robot": "R4", "setup": lambda: _apply("remove_recoveries", "W7"),
     "expect": ("request_review", None), "why": "Without the W7 recoveries, W7 has only a good signal. That is not enough to recommend it."},
    {"name": "Counterfactual: evidence moves to W4", "robot": "R4",
     "setup": lambda: query("MATCH (i:Incident)-[v:RESOLVED_VIA]->(:Location {id: 'W7'}) MATCH (w:Location {id: 'W4'}) DELETE v CREATE (i)-[:RESOLVED_VIA]->(w)"),
     "expect": ("reroute", "W4"), "why": "The same recoveries now point at W4, so the recommendation follows them."},
    {"name": "Guard: tempting route rejected", "robot": "R4", "guard": {"action": "reroute", "target_location_id": "W6", "supporting_incident_ids": ["INC-101"]},
     "why": "W6 is adjacent but has a failed incident and poor signal, so record_decision must refuse it."},
    {"name": "Guard: action must match the problem", "robot": "R4", "guard": {"action": "hold_and_service", "supporting_incident_ids": ["INC-106"]},
     "why": "Servicing a motor is not a response to communication loss, and the cited incident is a motor incident."},
]


def evaluate():
    results = []
    for s in SCENARIOS:
        with isolated_copy():
            if s.get("setup"):
                s["setup"]()
            row = {"name": s["name"], "robot": s["robot"], "why": s["why"]}
            if "guard" in s:
                res = tools.call_tool("record_decision", {"robot_id": s["robot"], "summary": "guard check", **s["guard"]})
                row.update(expected="rejected", actual="rejected" if "error" in res else "accepted", detail=res.get("error", ""))
                row["passed"] = "error" in res
            else:
                rec = assess.assess_incident(s["robot"])["recommended"]
                want = s["expect"]
                row.update(expected=_label({"action": want[0], "target": want[1]}), actual=_label(rec), detail=rec["basis"])
                row["passed"] = (rec["action"], rec["target"]) == want
            results.append(row)
    passed = sum(r["passed"] for r in results)
    return {"results": results, "passed": passed, "total": len(results),
            "note": "Each scenario runs on an isolated copy of the graph. This checks the evidence queries and validation rules, not the language model."}
