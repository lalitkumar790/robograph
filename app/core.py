import re

from app.db import query

ROBOT_RE = re.compile(r"^R\d{1,3}$")
LOC_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
INC_RE = re.compile(r"^INC-\d{1,6}$")
DECISION_RE = re.compile(r"^D-[0-9a-f]{8}$")
INCIDENT_TYPES = ("comm_loss", "motor_overheat")
ACTIONS = ("reroute", "hold_and_service", "return_to_dock", "request_review", "continue_monitoring")
ALLOWED = {
    "comm_loss": {"reroute", "return_to_dock", "request_review"},
    "motor_overheat": {"hold_and_service", "return_to_dock", "request_review"},
    None: {"continue_monitoring", "request_review"},
}
RSSI_DEGRADED = -80
GOOD_RSSI = -70
TEMP_HIGH = 75


class ToolError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean_text(text, limit):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def robot(robot_id):
    if not isinstance(robot_id, str) or not ROBOT_RE.match(robot_id):
        raise ToolError("robot_id must look like R4")
    rows = query(
        "MATCH (r:Robot {id: $id}) OPTIONAL MATCH (r)-[:LOCATED_AT]->(l:Location) "
        "RETURN r.id AS id, r.name AS name, r.model AS model, l.id AS location",
        {"id": robot_id},
    )
    if not rows:
        raise ToolError(f"robot {robot_id} not found", 404)
    return rows[0]


def location(location_id):
    if not isinstance(location_id, str) or not LOC_RE.match(location_id):
        raise ToolError("location_id must be alphanumeric, e.g. W5")
    rows = query("MATCH (l:Location {id: $id}) RETURN l.id AS id, l.name AS name, l.zone AS zone", {"id": location_id})
    if not rows:
        raise ToolError(f"location {location_id} not found", 404)
    return rows[0]


def telemetry(robot_id, limit):
    return query(
        "MATCH (:Robot {id: $id})-[:GENERATED]->(t:Telemetry)-[:RECORDED_AT]->(l:Location) "
        "RETURN t.ts AS ts, t.rssi AS rssi, t.battery AS battery, t.temp_c AS temp_c, l.id AS location "
        "ORDER BY ts DESC LIMIT $n",
        {"id": robot_id, "n": limit},
    )


def issues(t):
    out = []
    if t["rssi"] <= RSSI_DEGRADED:
        out.append({"type": "comm_loss", "detail": f"RSSI {t['rssi']} dBm (threshold {RSSI_DEGRADED})"})
    if t["temp_c"] >= TEMP_HIGH:
        out.append({"type": "motor_overheat", "detail": f"motor temperature {t['temp_c']} C (threshold {TEMP_HIGH})"})
    return out
