import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeout

from app import assess, experiments, seed, tools
from app.agent import investigate
from app.db import GRAPH_NAME, query

STATIC = Path(__file__).parent / "static"
RATE_LIMIT, RATE_WINDOW, MAX_BODY = 10, 60, 4096
_hits = defaultdict(deque)


class LLMNotConfigured(Exception):
    pass


def llm_client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise LLMNotConfigured()
    return OpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL") or None,
                  timeout=float(os.getenv("LLM_TIMEOUT", "120")), max_retries=0)


def graph_ready():
    seed.ensure()


@asynccontextmanager
async def lifespan(app):
    try:
        seed.ensure()
        experiments.drop_stale_copies()
    except (RedisConnectionError, RedisTimeout):
        print("FalkorDB not reachable yet; will retry on first request")
    yield


app = FastAPI(title="RoboGraph", lifespan=lifespan, docs_url=None, redoc_url=None)


def error(status, message):
    return JSONResponse({"error": message}, status_code=status)


@app.middleware("http")
async def guard(request: Request, call_next):
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return error(413, "request body too large")
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(tools.ToolError)
async def _tool_error(_, e):
    return error(e.status, str(e))


@app.exception_handler(RedisConnectionError)
@app.exception_handler(RedisTimeout)
async def _db_down(_, e):
    return error(503, "FalkorDB is unavailable")


@app.exception_handler(LLMNotConfigured)
async def _no_llm(_, e):
    return error(503, "OPENAI_API_KEY is not set on the server")


class InvestigateRequest(BaseModel):
    robot_id: str = Field(pattern=r"^R\d{1,3}$")
    question: str | None = Field(default=None, max_length=300)


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class ApprovalRequest(BaseModel):
    approve: bool


class OutcomeRequest(BaseModel):
    result: str = Field(pattern=r"^(recovered|not_recovered)$")


class CounterfactualRequest(BaseModel):
    robot_id: str = Field(pattern=r"^R\d{1,3}$")
    change: str = Field(pattern=r"^[a-z_]{1,30}$")
    location_id: str = Field(pattern=r"^[A-Za-z0-9]{1,8}$")
    use_agent: bool = False


class RobotRequest(BaseModel):
    id: str = Field(pattern=r"^R\d{1,3}$")
    name: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=40)
    location_id: str = Field(pattern=r"^[A-Za-z0-9]{1,8}$")


class TelemetryRequest(BaseModel):
    robot_id: str = Field(pattern=r"^R\d{1,3}$")
    location_id: str = Field(pattern=r"^[A-Za-z0-9]{1,8}$")
    rssi: int = Field(ge=-120, le=0)
    battery: int = Field(ge=0, le=100)
    temp_c: float = Field(ge=-20, le=150)


def rate_limit(request: Request):
    now, hits = time.monotonic(), _hits[request.client.host if request.client else "?"]
    while hits and now - hits[0] > RATE_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        raise tools.ToolError(f"rate limit: {RATE_LIMIT} heavy requests per {RATE_WINDOW}s", 429)
    hits.append(now)


@app.get("/api/health")
def health():
    seed.ensure()
    nodes = query("MATCH (n) RETURN count(n) AS n")[0]["n"]
    return {"status": "ok", "graph": GRAPH_NAME, "nodes": nodes, "llm_configured": bool(os.getenv("OPENAI_API_KEY")),
            "budget_s": float(os.getenv("INVESTIGATION_BUDGET_S", "180"))}


@app.get("/api/robots", dependencies=[Depends(graph_ready)])
def robots():
    return tools.list_robots()


@app.get("/api/robots/{robot_id}", dependencies=[Depends(graph_ready)])
def robot(robot_id: str):
    return tools.get_robot_context(robot_id)


@app.get("/api/robots/{robot_id}/memory", dependencies=[Depends(graph_ready)])
def memory(robot_id: str):
    return tools.get_robot_memory(robot_id)


@app.post("/api/robots/{robot_id}/notes", dependencies=[Depends(graph_ready)])
def note(robot_id: str, body: NoteRequest):
    return tools.add_note(robot_id, body.text)


@app.get("/api/decisions/{decision_id}/trace", dependencies=[Depends(graph_ready)])
def decision_trace(decision_id: str):
    return tools.trace_decision(decision_id)


@app.get("/api/robots/{robot_id}/assessment", dependencies=[Depends(graph_ready)])
def assessment(robot_id: str):
    cypher = []
    result = assess.assess_incident(robot_id, cypher)
    return {**result, "cypher": cypher}


@app.post("/api/robots", dependencies=[Depends(graph_ready)], status_code=201)
def add_robot(body: RobotRequest):
    return tools.register_robot(body.id, body.name, body.model, body.location_id)


@app.post("/api/telemetry", dependencies=[Depends(graph_ready)], status_code=201)
def add_telemetry(body: TelemetryRequest):
    return tools.add_telemetry(body.robot_id, body.location_id, body.rssi, body.battery, body.temp_c)


@app.post("/api/decisions/{decision_id}/approval", dependencies=[Depends(graph_ready)])
def approval(decision_id: str, body: ApprovalRequest):
    return tools.set_approval(decision_id, body.approve)


@app.post("/api/decisions/{decision_id}/executed", dependencies=[Depends(graph_ready)])
def executed(decision_id: str):
    return tools.set_executed(decision_id)


@app.post("/api/decisions/{decision_id}/outcome", dependencies=[Depends(graph_ready)])
def outcome(decision_id: str, body: OutcomeRequest):
    return tools.set_outcome(decision_id, body.result)


@app.post("/api/investigate", dependencies=[Depends(graph_ready), Depends(rate_limit)])
def run_investigation(body: InvestigateRequest):
    tools.get_robot_context(body.robot_id)
    return investigate(llm_client(), body.robot_id, body.question)


@app.post("/api/counterfactual", dependencies=[Depends(graph_ready), Depends(rate_limit)])
def run_counterfactual(body: CounterfactualRequest):
    return experiments.counterfactual(body.robot_id, body.change, body.location_id, llm_client() if body.use_agent else None)


@app.post("/api/evaluate", dependencies=[Depends(graph_ready), Depends(rate_limit)])
def run_evaluation():
    return experiments.evaluate()


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
