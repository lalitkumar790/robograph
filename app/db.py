import contextvars
import os
from contextlib import contextmanager

from falkordb import FalkorDB

GRAPH_NAME = os.getenv("FALKORDB_GRAPH", "robograph")
_client = None
_active = contextvars.ContextVar("graph_name", default=GRAPH_NAME)


def client():
    global _client
    if _client is None:
        _client = FalkorDB(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            password=os.getenv("FALKORDB_PASSWORD") or None,
        )
    return _client


def graph(name=None):
    return client().select_graph(name or _active.get())


@contextmanager
def use_graph(name):
    token = _active.set(name)
    try:
        yield
    finally:
        _active.reset(token)


def query(cypher, params=None):
    res = graph().query(cypher, params or {})
    cols = [h[1] for h in res.header]
    return [dict(zip(cols, row)) for row in res.result_set]
