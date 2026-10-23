import logging
import time
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeout

from app import assess
from app.core import (ACTIONS, ALLOWED, DECISION_RE, INC_RE, INCIDENT_TYPES, ROBOT_RE, ToolError, clean_text, issues, location,
                      robot, telemetry)
from app.db import query

log = logging.getLogger("robograph")
MAX_ROBOTS = 50
DEDUPE_S = 120


def list_robots():
    robots = query(
        "MATCH (r:Robot) OPTIONAL MATCH (r)-[:LOCATED_AT]->(l:Location) "
        "RETURN r.id AS id, r.name AS name, r.model AS model, l.id AS location ORDER BY id"
    )
    for r in robots:
        latest = telemetry(r["id"], 1)
        r["telemetry"] = latest[0] if latest else None
        found = issues(latest[0]) if latest else []
        r["status"] = found[0]["type"] if found else "ok"
    return robots


def register_robot(robot_id, name, model, location_id):
    if not isinstance(robot_id, str) or not ROBOT_RE.match(robot_id):
        raise ToolError("robot id must look like R5")
    location(location_id)
    if query("MATCH (r:Robot) RETURN count(r) AS n")[0]["n"] >= MAX_ROBOTS:
        raise ToolError(f"fleet is limited to {MAX_ROBOTS} robots", 409)
    if query("MATCH (r:Robot {id: $id}) RETURN r.id AS id", {"id": robot_id}):
        raise ToolError(f"robot {robot_id} already exists", 409)
    query("MATCH (l:Location {id: $l}) CREATE (:Robot {id: $id, name: $name, model: $model})-[:LOCATED_AT]->(l)",
          {"l": location_id, "id": robot_id, "name": clean_text(name, 40) or robot_id, "model": clean_text(model, 40) or "unknown"})
    return {"id": robot_id, "location": location_id}


def add_telemetry(robot_id, location_id, rssi, battery, temp_c):
    robot(robot_id)
    location(location_id)
    query("MATCH (r:Robot {id: $rid})-[old:LOCATED_AT]->() DELETE old", {"rid": robot_id})
    query("MATCH (r:Robot {id: $rid}), (l:Location {id: $l}) "
          "CREATE (r)-[:LOCATED_AT]->(l) "
          "CREATE (r)-[:GENERATED]->(:Telemetry {id: $id, ts: $ts, rssi: $rssi, battery: $battery, temp_c: $temp})-[:RECORDED_AT]->(l)",
          {"rid": robot_id, "l": location_id, "id": f"T-{uuid.uuid4().hex[:8]}", "ts": int(time.time()),
           "rssi": rssi, "battery": battery, "temp": temp_c})
    return {"robot": robot_id, "issues": issues({"rssi": rssi, "temp_c": temp_c})}


def get_robot_context(robot_id):
    r = robot(robot_id)
    latest = telemetry(robot_id, 3)
    loc = r["location"]
    neighbors = []
    if loc:
        neighbors = [x["id"] for x in query(
            "MATCH (:Location {id: $l})-[:CONNECTS_TO]-(n:Location) RETURN n.id AS id ORDER BY id", {"l": loc})]
    mission = query(
        "MATCH (:Robot {id: $id})-[:EXECUTED]->(m:Mission {status: 'active'})-[p:PASSED_THROUGH]->(l:Location) "
        "RETURN m.id AS id, l.id AS loc, p.seq AS seq ORDER BY seq", {"id": robot_id})
    incidents = query(
        "MATCH (:Robot {id: $id})-[:EXPERIENCED]->(i:Incident)-[:OCCURRED_AT]->(l:Location) "
        "RETURN i.id AS id, i.type AS type, l.id AS location, i.outcome AS outcome, i.ts AS ts ORDER BY ts DESC LIMIT 5",
        {"id": robot_id})
    return {
        "robot": {"id": r["id"], "name": r["name"], "model": r["model"]},
        "location": loc,
        "adjacent_locations": neighbors,
        "telemetry_latest_first": latest,
        "issues": issues(latest[0]) if latest else [],
        "active_mission": {"id": mission[0]["id"], "route": [m["loc"] for m in mission]} if mission else None,
        "robot_incident_history": incidents,
    }


def get_location_signal(location_id):
    location(location_id)
    rows = query(
        "MATCH (r:Robot)-[:GENERATED]->(t:Telemetry)-[:RECORDED_AT]->(:Location {id: $id}) "
        "RETURN r.id AS robot, avg(t.rssi) AS avg_rssi, min(t.rssi) AS min_rssi, count(t) AS samples ORDER BY robot",
        {"id": location_id})
    incidents = query("MATCH (i:Incident)-[:OCCURRED_AT]->(:Location {id: $id}) RETURN i.type AS type, count(i) AS n",
                      {"id": location_id})
    samples = sum(r["samples"] for r in rows)
    avg = round(sum(r["avg_rssi"] * r["samples"] for r in rows) / samples, 1) if samples else None
    for r in rows:
        r["avg_rssi"] = round(r["avg_rssi"], 1)
    return {"location": location_id, "avg_rssi": avg, "samples": samples, "by_robot": rows,
            "incidents_by_type": {r["type"]: r["n"] for r in incidents}}


def find_similar_incidents(incident_type, location_id=None):
    if incident_type not in INCIDENT_TYPES:
        raise ToolError(f"incident_type must be one of {', '.join(INCIDENT_TYPES)}")
    where, params = "", {"type": incident_type}
    if location_id:
        location(location_id)
        where, params["loc"] = "WHERE l.id = $loc", location_id
    rows = query(
        "MATCH (r:Robot)-[:EXPERIENCED]->(i:Incident {type: $type})-[:OCCURRED_AT]->(l:Location) " + where + " "
        "OPTIONAL MATCH (i)-[:RESOLVED_VIA]->(v:Location) OPTIONAL MATCH (i)-[:RESOLVED_BY]->(e:Engineer) "
        "OPTIONAL MATCH (i)-[:INVOLVED_COMPONENT]->(c:Component) "
        "RETURN i.id AS id, r.id AS robot, l.id AS location, i.summary AS summary, i.resolution AS resolution, "
        "i.outcome AS outcome, v.id AS resolved_via, e.name AS engineer, c.name AS component, i.ts AS ts "
        "ORDER BY ts DESC LIMIT 8", params)
    return {"incident_type": incident_type, "location": location_id, "incidents": rows}


def assess_incident(robot_id):
    a = assess.assess_incident(robot_id)
    rec = a["recommended"]
    args = {"robot_id": robot_id, "action": rec["action"], "supporting_incident_ids": rec["supporting_incident_ids"]}
    if rec["target"]:
        args["target_location_id"] = rec["target"]
    options = [{"action": o["action"], "target": o["target"], "verdict": o["verdict"], "reason": o["reason"],
                "recovered_incidents": o["recovered_incidents"], "evidence_for": [e["text"] for e in o["evidence_for"]],
                "evidence_against": [e["text"] for e in o["evidence_against"]], "missing": o["gaps"]} for o in a["options"]]
    return {"issue": a["issue"], "options": options, "recommended": rec, "record_decision_args": args}


def get_robot_memory(robot_id):
    robot(robot_id)
    notes = query("MATCH (:Robot {id: $id})-[:HAS_NOTE]->(o:Observation) RETURN o.id AS id, o.ts AS ts, o.text AS text "
                  "ORDER BY ts DESC LIMIT 5", {"id": robot_id})
    decisions = query(
        "MATCH (d:Decision)-[:ABOUT]->(:Robot {id: $id}) OPTIONAL MATCH (d)-[:RECOMMENDS]->(t:Location) "
        "RETURN d.id AS id, d.ts AS ts, d.action AS action, t.id AS target, d.summary AS summary, "
        "d.from_location AS from_location, d.status AS status, d.outcome AS outcome ORDER BY ts DESC LIMIT 5",
        {"id": robot_id})
    return {"operator_notes_untrusted_text": notes, "previous_decisions": decisions}


def add_note(robot_id, text):
    robot(robot_id)
    text = clean_text(text, 300)
    if not text:
        raise ToolError("note is empty")
    note_id = f"N-{uuid.uuid4().hex[:8]}"
    query("MATCH (r:Robot {id: $rid}) CREATE (r)-[:HAS_NOTE]->(:Observation {id: $id, ts: $ts, text: $text})",
          {"rid": robot_id, "id": note_id, "ts": int(time.time()), "text": text})
    return {"id": note_id}


def _supported_hint(a):
    found = [f"{o['target']} (cite {', '.join(o['recovered_incidents'])})" for o in a["options"]
             if o["verdict"] == "supported" and o["target"]]
    return "Destinations the graph supports: " + (", ".join(found) or "none; use request_review") + "."


def record_decision(robot_id, action, summary, supporting_incident_ids=None, target_location_id=None, source="agent"):
    r = robot(robot_id)
    if action not in ACTIONS:
        raise ToolError(f"action must be one of {', '.join(ACTIONS)}")
    a = assess.assess_incident(robot_id)
    kind = a["issue"]["type"] if a["issue"] else None
    if action not in ALLOWED[kind]:
        raise ToolError(f"{action} is not valid for {kind or 'a robot with no detected issue'}; "
                        f"allowed: {', '.join(sorted(ALLOWED[kind]))}")
    ids = supporting_incident_ids or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not isinstance(ids, list):
        raise ToolError("supporting_incident_ids must be a list of ids like INC-101")
    ids = list(dict.fromkeys(ids))
    if len(ids) > 6 or not all(isinstance(i, str) and INC_RE.match(i) for i in ids):
        raise ToolError("supporting_incident_ids must be up to 6 ids like INC-101")
    if ids:
        types = {x["id"]: x["type"] for x in query("MATCH (i:Incident) WHERE i.id IN $ids RETURN i.id AS id, i.type AS type", {"ids": ids})}
        missing = [i for i in ids if i not in types]
        if missing:
            raise ToolError(f"unknown incident ids: {', '.join(missing)}")
        if kind and any(t != kind for t in types.values()):
            raise ToolError(f"cited incidents must be {kind} incidents for this robot")
    if action in ("reroute", "hold_and_service", "return_to_dock") and not ids:
        raise ToolError("cite at least one supporting incident from the graph. " + _supported_hint(a))

    if action == "reroute":
        if not target_location_id:
            raise ToolError("reroute needs target_location_id. " + _supported_hint(a))
        location(target_location_id)
        option = next((o for o in a["options"] if o["target"] == target_location_id), None)
        if option is None:
            raise ToolError(f"{target_location_id} is not adjacent to {r['location']}; "
                            f"adjacent: {', '.join(sorted(o['target'] for o in a['options']))}. " + _supported_hint(a))
        _require_supported(option, target_location_id, ids, a)
    elif action == "hold_and_service":
        _require_supported(a["options"][0], "servicing", ids, a)
        if target_location_id:
            raise ToolError("target_location_id is only valid for reroute")
    elif target_location_id:
        raise ToolError("target_location_id is only valid for reroute")
    summary = clean_text(summary, 400)
    if not summary:
        raise ToolError("summary is empty")

    same = query("MATCH (d:Decision {status: 'recorded', action: $a})-[:ABOUT]->(:Robot {id: $rid}) WHERE d.ts >= $since "
                 "OPTIONAL MATCH (d)-[:RECOMMENDS]->(t:Location) RETURN d.id AS id, t.id AS target ORDER BY d.ts DESC",
                 {"a": action, "rid": robot_id, "since": int(time.time()) - DEDUPE_S})
    duplicate = next((x for x in same if x["target"] == target_location_id), None)
    if duplicate:
        return {"decision_id": duplicate["id"], "status": "already_recorded"}

    latest = (telemetry(robot_id, 1) or [None])[0]
    decision_id = f"D-{uuid.uuid4().hex[:8]}"
    query("MATCH (r:Robot {id: $rid}) "
          "CREATE (d:Decision {id: $id, ts: $ts, action: $action, summary: $summary, status: 'recorded', issue_type: $kind, source: $source, "
          "from_location: $loc, from_rssi: $rssi, from_temp_c: $temp}) CREATE (d)-[:ABOUT]->(r)",
          {"rid": robot_id, "id": decision_id, "ts": int(time.time()), "action": action, "summary": summary, "kind": kind,
           "source": source, "loc": r["location"], "rssi": latest["rssi"] if latest else None, "temp": latest["temp_c"] if latest else None})
    if ids:
        query("UNWIND $ids AS iid MATCH (d:Decision {id: $id}), (i:Incident {id: iid}) CREATE (d)-[:SUPPORTED_BY]->(i)",
              {"id": decision_id, "ids": ids})
    if target_location_id:
        query("MATCH (d:Decision {id: $id}), (l:Location {id: $l}) CREATE (d)-[:RECOMMENDS]->(l)",
              {"id": decision_id, "l": target_location_id})
    return {"decision_id": decision_id, "status": "recorded"}


def _require_supported(option, name, cited, a):
    if option["verdict"] != "supported":
        why = "; ".join([option["reason"]] + option["gaps"] + [e["text"] for e in option["evidence_against"]])
        raise ToolError(f"{name} is '{option['verdict']}' in the graph, not supported: {why}. " + _supported_hint(a))
    if not set(cited) & set(option["recovered_incidents"]):
        raise ToolError(f"cite incidents that recovered this way: {', '.join(option['recovered_incidents'])}")


def _decision_state(decision_id):
    if not isinstance(decision_id, str) or not DECISION_RE.match(decision_id):
        raise ToolError("invalid decision id")
    rows = query("MATCH (d:Decision {id: $id}) RETURN d.status AS status, d.outcome AS outcome", {"id": decision_id})
    if not rows:
        raise ToolError(f"decision {decision_id} not found", 404)
    return rows[0]


def _transition(decision_id, expected, fields, message):
    if _decision_state(decision_id)["status"] != expected:
        raise ToolError(message, 409)
    query("MATCH (d:Decision {id: $id}) SET d += $fields", {"id": decision_id, "fields": {**fields, "updated_ts": int(time.time())}})


def set_approval(decision_id, approve):
    status = "approved" if approve else "rejected"
    _transition(decision_id, "recorded", {"status": status}, "only a recorded decision can be approved or rejected")
    return {"decision_id": decision_id, "status": status}


def set_executed(decision_id):
    _transition(decision_id, "approved", {"status": "executed"}, "only an approved decision can be marked executed")
    return {"decision_id": decision_id, "status": "executed"}


def set_outcome(decision_id, result):
    if result not in ("recovered", "not_recovered"):
        raise ToolError("result must be recovered or not_recovered")
    state = _decision_state(decision_id)
    if state["status"] != "executed":
        raise ToolError("only an executed decision can have an outcome", 409)
    if state["outcome"]:
        raise ToolError("outcome already recorded", 409)
    query("MATCH (d:Decision {id: $id}) SET d.outcome = $o, d.outcome_ts = $ts", {"id": decision_id, "o": result, "ts": int(time.time())})
    return {"decision_id": decision_id, "outcome": result}


def trace_decision(decision_id):
    _decision_state(decision_id)
    d = query(
        "MATCH (d:Decision {id: $id})-[:ABOUT]->(r:Robot) OPTIONAL MATCH (d)-[:RECOMMENDS]->(t:Location) "
        "RETURN d.id AS id, d.ts AS ts, d.action AS action, d.summary AS summary, d.status AS status, d.outcome AS outcome, "
        "d.from_location AS from_location, d.from_rssi AS rssi, d.from_temp_c AS temp_c, d.source AS source, r.id AS robot, t.id AS target",
        {"id": decision_id})[0]
    incidents = query(
        "MATCH (:Decision {id: $id})-[:SUPPORTED_BY]->(i:Incident)<-[:EXPERIENCED]-(r:Robot) "
        "OPTIONAL MATCH (i)-[:OCCURRED_AT]->(l:Location) OPTIONAL MATCH (i)-[:RESOLVED_VIA]->(v:Location) "
        "OPTIONAL MATCH (i)-[:RESOLVED_BY]->(e:Engineer) OPTIONAL MATCH (i)-[:INVOLVED_COMPONENT]->(c:Component) "
        "RETURN i.id AS id, i.type AS type, i.outcome AS outcome, i.resolution AS resolution, r.id AS robot, "
        "l.id AS location, v.id AS resolved_via, e.id AS engineer_id, e.name AS engineer, c.name AS component ORDER BY id",
        {"id": decision_id})

    nodes, edges = {}, []

    def node(id_, kind, label=None):
        nodes.setdefault(id_, {"id": id_, "kind": kind, "label": label or id_})

    def edge(a, b, rel):
        e = {"source": a, "target": b, "type": rel}
        if e not in edges:
            edges.append(e)

    steps = []
    here = d["from_location"]
    reading = []
    if d["rssi"] is not None:
        reading.append(f"RSSI {d['rssi']} dBm")
    if d["temp_c"] is not None:
        reading.append(f"motor {d['temp_c']} C")
    steps.append({"kind": "robot", "title": f"{d['robot']} at {here or 'unknown location'}",
                  "detail": ", ".join(reading) or "no telemetry recorded", "refs": [d["robot"], here]})
    node(d["robot"], "robot")
    node("D:" + d["id"], "decision", d["id"])
    edge("D:" + d["id"], d["robot"], "ABOUT")
    if here:
        node(here, "location")
        edge(d["robot"], here, "LOCATED_AT")
        sig = get_location_signal(here)
        if sig["samples"]:
            robots = ", ".join(r["robot"] for r in sig["by_robot"])
            inc = ", ".join(f"{n} {t}" for t, n in sig["incidents_by_type"].items()) or "no incidents"
            steps.append({"kind": "location", "title": f"{here} history",
                          "detail": f"avg RSSI {sig['avg_rssi']} dBm over {sig['samples']} readings from {robots}; {inc}",
                          "refs": [here]})
    for i in incidents:
        outcome = f"{i['outcome']}" + (f" via {i['resolved_via']}" if i["resolved_via"] else "")
        steps.append({"kind": "incident", "title": f"{i['id']}: {i['robot']} {i['type']} at {i['location']}",
                      "detail": f"{outcome}, {i['engineer']}. {i['resolution']}", "refs": [i["id"], i["robot"]]})
        node(i["id"], "incident")
        node(i["robot"], "robot")
        edge(i["robot"], i["id"], "EXPERIENCED")
        edge("D:" + d["id"], i["id"], "SUPPORTED_BY")
        if i["location"]:
            node(i["location"], "location")
            edge(i["id"], i["location"], "OCCURRED_AT")
        if i["resolved_via"]:
            node(i["resolved_via"], "location")
            edge(i["id"], i["resolved_via"], "RESOLVED_VIA")
        if i["engineer_id"]:
            node(i["engineer_id"], "engineer", i["engineer"])
            edge(i["id"], i["engineer_id"], "RESOLVED_BY")
    if d["target"]:
        sig = get_location_signal(d["target"])
        done = query("MATCH (m:Mission {status: 'completed'})-[:PASSED_THROUGH]->(:Location {id: $l}) RETURN count(m) AS n",
                     {"l": d["target"]})[0]["n"]
        steps.append({"kind": "target", "title": f"{d['target']} history",
                      "detail": f"avg RSSI {sig['avg_rssi']} dBm over {sig['samples']} readings; "
                                f"{sum(sig['incidents_by_type'].values())} incident(s); {done} completed mission(s) passed through",
                      "refs": [d["target"]]})
        node(d["target"], "location")
        edge("D:" + d["id"], d["target"], "RECOMMENDS")
        if here and query("MATCH (:Location {id: $a})-[c:CONNECTS_TO]-(:Location {id: $b}) RETURN count(c) AS n",
                          {"a": here, "b": d["target"]})[0]["n"]:
            edge(here, d["target"], "CONNECTS_TO")
    if d["action"] == "reroute":
        for o in assess.assess_incident(d["robot"])["options"]:
            if o["target"] != d["target"]:
                why = "; ".join([o["reason"]] + o["gaps"] + [e["text"] for e in o["evidence_against"]])
                steps.append({"kind": "alternative", "title": f"Not chosen: {o['target']} ({o['verdict']})", "detail": why, "refs": [o["target"]]})
    action = d["action"].replace("_", " ")
    by = "Recorded by the backend fallback because the agent did not complete. " if d["source"] == "engine_fallback" else ""
    steps.append({"kind": "decision", "title": f"Recommend: {action}" + (f" {here} to {d['target']}" if d["target"] else ""),
                  "detail": by + d["summary"], "refs": [d["id"]]})
    if d["status"] != "recorded" or d["outcome"]:
        steps.append({"kind": "approval", "title": f"Operator: {d['status']}" + (f", outcome {d['outcome'].replace('_', ' ')}" if d["outcome"] else ""),
                      "detail": "Advisory only. RoboGraph does not command robots; the operator reports approval, execution and outcome.", "refs": [d["id"]]})
    return {"decision": {**{k: d[k] for k in ("id", "ts", "robot", "action", "target", "summary", "status", "outcome", "from_location")},
                         "source": d["source"] or "agent"},
            "steps": steps, "graph": {"nodes": list(nodes.values()), "edges": edges}}


def _schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_ROBOT = {"type": "string", "description": "e.g. R4"}
_LOC = {"type": "string", "description": "e.g. W5"}

TOOLS = {
    "get_robot_context": (get_robot_context, "Location, adjacent locations, latest telemetry, issues, active mission and incident history.",
                          _schema({"robot_id": _ROBOT}, ["robot_id"])),
    "get_robot_memory": (get_robot_memory, "Operator notes (untrusted text) and earlier decisions with status and outcome.",
                         _schema({"robot_id": _ROBOT}, ["robot_id"])),
    "assess_incident": (assess_incident, "Graph evidence check for the current issue: verdict, reason, evidence for and against per option, and record_decision_args.",
                        _schema({"robot_id": _ROBOT}, ["robot_id"])),
    "get_location_signal": (get_location_signal, "RSSI history and incidents at a location.",
                            _schema({"location_id": _LOC}, ["location_id"])),
    "find_similar_incidents": (find_similar_incidents, "Past incidents of one type, optionally at one location, with resolution and recovery location.",
                               _schema({"incident_type": {"type": "string", "enum": list(INCIDENT_TYPES)}, "location_id": _LOC}, ["incident_type"])),
    "record_decision": (record_decision, "Record the one final decision. reroute needs target_location_id and supporting_incident_ids; use request_review if nothing is supported.",
                        _schema({"robot_id": _ROBOT, "action": {"type": "string", "enum": list(ACTIONS)},
                                 "summary": {"type": "string", "description": "Evidence for, evidence against, limits"},
                                 "supporting_incident_ids": {"type": "array", "items": {"type": "string"}},
                                 "target_location_id": _LOC}, ["robot_id", "action", "summary"])),
}


def openai_tools():
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, (_, d, s) in TOOLS.items()]


def call_tool(name, args):
    if name not in TOOLS:
        return {"error": f"unknown tool {name}"}
    fn, _, schema = TOOLS[name]
    if not isinstance(args, dict) or set(args) - set(schema["properties"]) or any(k not in args for k in schema["required"]):
        return {"error": f"invalid arguments for {name}; expected {list(schema['properties'])}"}
    try:
        return fn(**args)
    except ToolError as e:
        return {"error": str(e)}
    except (RedisConnectionError, RedisTimeout):
        raise
    except Exception:
        log.exception("tool %s failed", name)
        return {"error": f"{name} failed on the server; try different arguments"}
