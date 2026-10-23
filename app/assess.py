from app import core
from app.db import query

VERDICT_ORDER = {"supported": 0, "weak": 1, "conflicting": 2, "rejected": 3}
LIMITATIONS = [
    "The graph holds recorded incidents, telemetry and missions. It cannot show current traffic or obstacles at a target.",
    "Approval and execution are simulated; no physical robot is controlled.",
]


def _run(log, name, cypher, params):
    if log is not None:
        log.append({"name": name, "cypher": " ".join(cypher.split()), "params": params})
    return query(cypher, params)


def _item(text, *refs):
    return {"text": text, "refs": [r for r in refs if r]}


def _route_options(here, log):
    near = "MATCH (:Location {id: $here})-[:CONNECTS_TO]-(n:Location) "
    p = {"here": here}
    signal = _run(log, "signal_at_candidates", near +
                  "OPTIONAL MATCH (t:Telemetry)-[:RECORDED_AT]->(n) "
                  "RETURN n.id AS id, avg(t.rssi) AS avg_rssi, count(t) AS samples", p)
    failures = _run(log, "failed_incidents_at_candidates", near +
                    "MATCH (n)<-[:OCCURRED_AT]-(i:Incident {type: 'comm_loss'}) WHERE i.outcome <> 'recovered' "
                    "OPTIONAL MATCH (r:Robot)-[:EXPERIENCED]->(i) "
                    "RETURN n.id AS id, i.id AS incident, r.id AS robot, i.outcome AS outcome", p)
    recoveries = _run(log, "recoveries_from_here",
                      "MATCH (r:Robot)-[:EXPERIENCED]->(i:Incident {type: 'comm_loss'})-[:OCCURRED_AT]->(:Location {id: $here}), "
                      "(i)-[:RESOLVED_VIA]->(n:Location)-[:CONNECTS_TO]-(:Location {id: $here}) "
                      "RETURN n.id AS id, i.id AS incident, r.id AS robot ORDER BY incident", p)
    missions = _run(log, "missions_through_candidates", near +
                    "MATCH (n)<-[:PASSED_THROUGH]-(m:Mission) RETURN n.id AS id, m.status AS status, count(m) AS c", p)
    verified = _run(log, "verified_agent_outcomes", near +
                    "MATCH (n)<-[:RECOMMENDS]-(d:Decision {from_location: $here, action: 'reroute'}) "
                    "WHERE d.outcome IS NOT NULL RETURN n.id AS id, d.id AS decision, d.outcome AS outcome", p)

    options = []
    for s in signal:
        n = s["id"]
        avg = round(s["avg_rssi"], 1) if s["avg_rssi"] is not None else None
        rec = [r for r in recoveries if r["id"] == n]
        bad = [f for f in failures if f["id"] == n]
        won = [v for v in verified if v["id"] == n and v["outcome"] == "recovered"]
        lost = [v for v in verified if v["id"] == n and v["outcome"] != "recovered"]
        good = avg is not None and avg >= core.GOOD_RSSI
        pro = [_item(f"{r['incident']} ({r['robot']}) had comm_loss at {here} and recovered after moving to {n}", r["incident"], r["robot"]) for r in rec]
        pro += [_item(f"{v['decision']}: an approved reroute {here} to {n} was verified as recovered", v["decision"]) for v in won]
        con = [_item(f"{b['incident']} ({b['robot']}) had comm_loss at {n} and ended {b['outcome']}", b["incident"], b["robot"]) for b in bad]
        con += [_item(f"{v['decision']}: an approved reroute {here} to {n} was verified as not recovered", v["decision"]) for v in lost]
        gaps = []
        if avg is None:
            gaps.append(f"no telemetry recorded at {n}")
        elif good:
            pro.append(_item(f"avg RSSI {avg} dBm over {s['samples']} readings (good is {core.GOOD_RSSI} or higher)", n))
        else:
            con.append(_item(f"avg RSSI {avg} dBm over {s['samples']} readings (below {core.GOOD_RSSI})", n))
        for m in (m for m in missions if m["id"] == n):
            if m["status"] == "completed":
                pro.append(_item(f"{m['c']} completed mission(s) passed through {n}", n))
            elif m["status"] == "aborted":
                con.append(_item(f"{m['c']} aborted mission(s) passed through {n} (cause not recorded)", n))
        hard = len(bad) + len(lost)
        if not any(m["id"] == n and m["status"] == "completed" for m in missions):
            gaps.append(f"no completed mission has passed through {n}")
        if rec and hard:
            verdict, reason = "conflicting", f"recovered incidents exist but failures are also recorded at {n}"
        elif hard:
            verdict, reason = "rejected", f"failures are recorded at {n} and no incident recovered by moving there"
        elif rec and good:
            verdict, reason = "supported", f"{len(rec)} incident(s) recovered by moving from {here} to {n}, signal is good, no failures recorded"
        elif rec:
            verdict, reason = "weak", f"incidents recovered by moving to {n}, but the signal there is not good"
        elif good:
            verdict, reason = "weak", f"signal at {n} looks good, but no incident has recovered by moving there"
        else:
            verdict, reason = "weak", f"no recovery evidence for {n}" + ("" if avg is None else " and the signal there is not good")
        options.append({"action": "reroute", "target": n, "verdict": verdict, "reason": reason, "avg_rssi": avg,
                        "samples": s["samples"], "recovered_incidents": [r["incident"] for r in rec],
                        "evidence_for": pro, "evidence_against": con, "gaps": gaps})
    options.sort(key=lambda o: (VERDICT_ORDER[o["verdict"]], -len(o["recovered_incidents"]), -(o["avg_rssi"] or -999)))
    return options


def _service_options(log):
    rows = _run(log, "motor_incident_history",
                "MATCH (r:Robot)-[:EXPERIENCED]->(i:Incident {type: 'motor_overheat'}) "
                "OPTIONAL MATCH (i)-[:INVOLVED_COMPONENT]->(c:Component) OPTIONAL MATCH (i)-[:RESOLVED_BY]->(e:Engineer) "
                "RETURN i.id AS incident, r.id AS robot, i.outcome AS outcome, i.resolution AS resolution, "
                "c.name AS component, e.name AS engineer ORDER BY incident", {})
    rec = [r for r in rows if r["outcome"] == "recovered"]
    bad = [r for r in rows if r["outcome"] != "recovered"]
    if rec and bad:
        verdict, reason = "conflicting", "earlier motor incidents both recovered and failed"
    elif rec:
        verdict, reason = "supported", f"{len(rec)} earlier motor incident(s) recovered after servicing, none failed"
    elif bad:
        verdict, reason = "rejected", "earlier motor incidents ended without recovery"
    else:
        verdict, reason = "weak", "no earlier motor overheating incidents are recorded"
    return [{"action": "hold_and_service", "target": None, "verdict": verdict, "reason": reason,
             "recovered_incidents": [r["incident"] for r in rec],
             "evidence_for": [_item(f"{r['incident']} ({r['robot']}, {r['component']}): {r['resolution']} Resolved by {r['engineer']}.", r["incident"], r["robot"]) for r in rec],
             "evidence_against": [_item(f"{r['incident']} ({r['robot']}) motor overheat ended {r['outcome']}", r["incident"]) for r in bad],
             "gaps": [] if rows else ["no earlier motor overheating incidents recorded"]}]


def explain(options):
    if not options:
        return "No candidate option exists for this robot."
    return "No option is supported. " + "; ".join(f"{o['target'] or o['action'].replace('_', ' ')} is {o['verdict']} ({o['reason']})" for o in options) + "."


def assess_incident(robot_id, log=None):
    robot = core.robot(robot_id)
    latest = core.telemetry(robot_id, 1)
    found = core.issues(latest[0]) if latest else []
    out = {"robot": robot_id, "location": robot["location"], "issue": found[0] if found else None,
           "options": [], "limitations": list(LIMITATIONS)}
    if not found:
        out["recommended"] = {"action": "continue_monitoring", "target": None, "supporting_incident_ids": [],
                              "basis": "no telemetry threshold is exceeded"}
        if not latest:
            out["limitations"].append("no telemetry has been recorded for this robot")
        return out
    kind = found[0]["type"]
    if kind == "comm_loss" and not robot["location"]:
        out["limitations"].append("the robot has no known location, so adjacent routes cannot be checked")
        options = []
    else:
        options = _route_options(robot["location"], log) if kind == "comm_loss" else _service_options(log)
    out["options"] = options
    best = next((o for o in options if o["verdict"] == "supported"), None)
    if best:
        out["recommended"] = {"action": best["action"], "target": best["target"],
                              "supporting_incident_ids": best["recovered_incidents"],
                              "basis": f"{len(best['recovered_incidents'])} recovered incident(s) support this, with no failures recorded against it"}
    else:
        out["recommended"] = {"action": "request_review", "target": None, "supporting_incident_ids": [],
                              "basis": explain(options)}
    return out
