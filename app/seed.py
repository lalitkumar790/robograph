import sys
from datetime import datetime, timedelta, timezone

from redis.exceptions import ResponseError

from app.db import graph, query

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
D = 1440


def ts(minutes_ago):
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp())


ROBOTS = [
    ("R1", "Hauler 1", "Rover-A", "W2"),
    ("R2", "Hauler 2", "Rover-A", "W8"),
    ("R3", "Sorter 3", "Rover-B", "W6"),
    ("R4", "Hauler 4", "Rover-A", "W5"),
]

LOCATIONS = [
    ("Dock", "Charging dock", "Dock"),
    ("W1", "Waypoint 1", "West aisle"),
    ("W2", "Waypoint 2", "West aisle"),
    ("W3", "Waypoint 3", "West aisle"),
    ("W4", "Waypoint 4", "Central corridor"),
    ("W5", "Waypoint 5", "Central corridor"),
    ("W6", "Waypoint 6", "East bay"),
    ("W7", "Waypoint 7", "East bay"),
    ("W8", "Waypoint 8", "East bay"),
]

CONNECTIONS = [
    ("Dock", "W1"), ("W1", "W2"), ("W2", "W3"), ("W3", "W4"), ("W4", "W5"),
    ("W5", "W6"), ("W5", "W7"), ("W6", "W8"), ("W7", "W8"),
]

COMPONENTS = [("C-ANT", "Wi-Fi antenna"), ("C-MOT", "Drive motor")]
ENGINEERS = [("E-PRIYA", "Priya Nair"), ("E-ARJUN", "Arjun Rao")]

# robot, location, minutes ago, rssi dBm, battery %, motor temp C
TELEMETRY = [
    ("R4", "W3", 180, -56, 72, 40), ("R4", "W4", 120, -64, 71, 41),
    ("R4", "W5", 60, -82, 70, 42), ("R4", "W5", 30, -85, 70, 43), ("R4", "W5", 5, -88, 69, 43),
    ("R4", "W4", 6 * D + 60, -62, 81, 40),
    ("R4", "W5", 6 * D, -84, 80, 41), ("R4", "W5", 6 * D + 20, -87, 80, 41),
    ("R4", "W5", 2 * D, -83, 62, 42), ("R4", "W5", 2 * D + 15, -86, 62, 42),
    ("R2", "W5", 20 * D, -85, 70, 40), ("R2", "W5", 20 * D + 10, -88, 70, 40),
    ("R2", "W7", 20 * D - 30, -52, 69, 41), ("R2", "W7", 20 * D - 45, -54, 69, 41),
    ("R2", "W8", 20 * D - 90, -60, 67, 41),
    ("R2", "W5", 19 * D, -79, 66, 40), ("R2", "W7", 19 * D - 20, -53, 65, 41),
    ("R2", "W8", 20, -61, 55, 38),
    ("R1", "W5", 12 * D, -84, 75, 40), ("R1", "W5", 12 * D + 10, -86, 75, 40),
    ("R1", "W7", 12 * D - 25, -55, 73, 41), ("R1", "W7", 12 * D - 40, -53, 73, 41),
    ("R1", "W4", 11 * D + 30, -63, 90, 40), ("R1", "W5", 11 * D, -81, 88, 41),
    ("R1", "W2", 30 * D, -58, 85, 79), ("R1", "W2", 15, -57, 90, 39),
    ("R3", "W6", 9 * D, -84, 60, 45), ("R3", "W6", 9 * D + 10, -87, 60, 45),
    ("R3", "W6", 15 * D, -76, 50, 83),
    ("R3", "W6", 90, -76, 52, 70), ("R3", "W6", 45, -77, 51, 76), ("R3", "W6", 5, -78, 50, 81),
]

# id, robot, status, started minutes ago, route
MISSIONS = [
    ("M-201", "R2", "completed", 19 * D, ["W3", "W4", "W5", "W7", "W8"]),
    ("M-202", "R1", "completed", 11 * D, ["W3", "W4", "W5", "W7", "W8"]),
    ("M-203", "R3", "aborted", 9 * D, ["W4", "W5", "W6", "W8"]),
    ("M-204", "R4", "paused", 2 * D, ["W3", "W4", "W5"]),
    ("M-205", "R4", "active", 200, ["W3", "W4", "W5", "W6", "W8"]),
]

# id, robot, type, location, minutes ago, severity, summary, resolution, outcome, resolved via, component, engineer
INCIDENTS = [
    ("INC-101", "R2", "comm_loss", "W5", 20 * D, 3, "RSSI fell to -88 dBm at W5, mission paused.",
     "Antenna inspected, no fault. Rerouted via W7, link recovered.", "recovered", "W7", "C-ANT", "E-PRIYA"),
    ("INC-102", "R1", "comm_loss", "W5", 12 * D, 3, "RSSI fell to -86 dBm at W5.",
     "Rerouted via W7, link stable through W8.", "recovered", "W7", "C-ANT", "E-ARJUN"),
    ("INC-103", "R3", "comm_loss", "W6", 9 * D, 3, "Link dropped at W6 on route W5, W6, W8.",
     "Detour through W6 dropped the link again. Mission aborted, robot returned to dock.", "failed", None, "C-ANT", "E-ARJUN"),
    ("INC-104", "R4", "comm_loss", "W5", 6 * D, 2, "RSSI -87 dBm at W5.",
     "Radio restarted. Link returned briefly, degraded again on the next pass.", "temporary", None, "C-ANT", "E-PRIYA"),
    ("INC-105", "R4", "comm_loss", "W5", 2 * D, 3, "RSSI -86 dBm at W5, mission M-204 paused.",
     "Radio restarted again. Mission resumed manually from W4.", "temporary", None, "C-ANT", "E-ARJUN"),
    ("INC-106", "R3", "motor_overheat", "W6", 15 * D, 2, "Drive motor reached 83 C at W6.",
     "Worn bearing replaced, temperature back to normal.", "recovered", None, "C-MOT", "E-PRIYA"),
    ("INC-107", "R1", "motor_overheat", "W2", 30 * D, 2, "Drive motor reached 79 C at W2.",
     "Debris cleared from motor housing.", "recovered", None, "C-MOT", "E-ARJUN"),
]

NOTES = [
    ("N-001", "R4", D, "Signal drops reported near W5 on the last two shifts. Radio restarts only helped briefly."),
    ("N-002", "R3", 3 * 60, "Motor housing felt warm after the last shift."),
]


def reset():
    try:
        graph().delete()
    except ResponseError:
        pass


def seed():
    reset()
    for label in ("Robot", "Location", "Incident", "Decision", "Mission", "Telemetry"):
        try:
            query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
        except ResponseError:
            pass

    query("UNWIND $rows AS r CREATE (:Robot {id: r.id, name: r.name, model: r.model})",
          {"rows": [dict(id=i, name=n, model=m) for i, n, m, _ in ROBOTS]})
    query("UNWIND $rows AS r CREATE (:Location {id: r.id, name: r.name, zone: r.zone})",
          {"rows": [dict(id=i, name=n, zone=z) for i, n, z in LOCATIONS]})
    query("UNWIND $rows AS r CREATE (:Component {id: r.id, name: r.name})",
          {"rows": [dict(id=i, name=n) for i, n in COMPONENTS]})
    query("UNWIND $rows AS r CREATE (:Engineer {id: r.id, name: r.name})",
          {"rows": [dict(id=i, name=n) for i, n in ENGINEERS]})

    query("UNWIND $rows AS r MATCH (a:Location {id: r.a}), (b:Location {id: r.b}) CREATE (a)-[:CONNECTS_TO]->(b)",
          {"rows": [dict(a=a, b=b) for a, b in CONNECTIONS]})
    query("UNWIND $rows AS r MATCH (x:Robot {id: r.robot}), (l:Location {id: r.loc}) CREATE (x)-[:LOCATED_AT]->(l)",
          {"rows": [dict(robot=i, loc=loc) for i, _, _, loc in ROBOTS]})

    query("""UNWIND $rows AS t
             MATCH (r:Robot {id: t.robot}), (l:Location {id: t.loc})
             CREATE (r)-[:GENERATED]->(:Telemetry {id: t.id, ts: t.ts, rssi: t.rssi, battery: t.battery, temp_c: t.temp})-[:RECORDED_AT]->(l)""",
          {"rows": [dict(id=f"T-{n:03d}", robot=r, loc=l, ts=ts(m), rssi=rssi, battery=b, temp=t)
                    for n, (r, l, m, rssi, b, t) in enumerate(TELEMETRY, 1)]})

    query("""UNWIND $rows AS m
             MATCH (r:Robot {id: m.robot})
             CREATE (r)-[:EXECUTED]->(:Mission {id: m.id, status: m.status, started: m.ts})""",
          {"rows": [dict(id=i, robot=r, status=s, ts=ts(m)) for i, r, s, m, _ in MISSIONS]})
    query("""UNWIND $rows AS p
             MATCH (m:Mission {id: p.mission}), (l:Location {id: p.loc})
             CREATE (m)-[:PASSED_THROUGH {seq: p.seq}]->(l)""",
          {"rows": [dict(mission=i, loc=loc, seq=n) for i, _, _, _, route in MISSIONS for n, loc in enumerate(route)]})

    incidents = [dict(id=i, robot=r, type=t, loc=loc, ts=ts(m), severity=sev, summary=s, resolution=res,
                      outcome=o, via=v, component=c, engineer=e)
                 for i, r, t, loc, m, sev, s, res, o, v, c, e in INCIDENTS]
    query("""UNWIND $rows AS i
             MATCH (r:Robot {id: i.robot}), (l:Location {id: i.loc}), (c:Component {id: i.component}), (e:Engineer {id: i.engineer})
             CREATE (r)-[:EXPERIENCED]->(n:Incident {id: i.id, type: i.type, ts: i.ts, severity: i.severity,
                                                      summary: i.summary, resolution: i.resolution, outcome: i.outcome})
             CREATE (n)-[:OCCURRED_AT]->(l), (n)-[:INVOLVED_COMPONENT]->(c), (n)-[:RESOLVED_BY]->(e)""",
          {"rows": incidents})
    query("""UNWIND $rows AS i
             MATCH (n:Incident {id: i.id}), (v:Location {id: i.via})
             CREATE (n)-[:RESOLVED_VIA]->(v)""",
          {"rows": [i for i in incidents if i["via"]]})

    query("""UNWIND $rows AS n
             MATCH (r:Robot {id: n.robot})
             CREATE (r)-[:HAS_NOTE]->(:Observation {id: n.id, ts: n.ts, text: n.text})""",
          {"rows": [dict(id=i, robot=r, ts=ts(m), text=t) for i, r, m, t in NOTES]})


_ready = False


def ensure():
    global _ready
    if _ready:
        return
    if query("MATCH (r:Robot) RETURN count(r) AS n")[0]["n"] == 0:
        seed()
    else:
        query("MATCH (d:Decision {status: 'proposed'}) SET d.status = 'recorded'")
    _ready = True


if __name__ == "__main__":
    seed()
    print("seeded", query("MATCH (n) RETURN count(n) AS nodes")[0]["nodes"], "nodes")
    sys.exit(0)
