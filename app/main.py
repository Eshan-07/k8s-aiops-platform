"""Orders API: a small service we deploy, monitor, break on purpose, and later
analyse with anomaly detection and an LLM incident assistant.

Chaos endpoints only work when CHAOS_ENABLED=true (learning project only).
"""
import asyncio
import os
import random
import threading
import time
from itertools import count

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

app = FastAPI(title="Orders API", version="0.1.0")

# ---------------------------------------------------------------- metrics
REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
IN_PROGRESS = Gauge("http_requests_in_progress", "Requests currently being served")
ORDERS_CREATED = Counter("orders_created_total", "Orders created")

# ------------------------------------------------------------ chaos state
CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() == "true"


class ChaosState:
    latency_ms = 0
    error_percent = 0
    ready = True


chaos = ChaosState()


def chaos_allowed() -> None:
    if not CHAOS_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


async def apply_chaos() -> None:
    """Injects delay and random errors into business endpoints."""
    if chaos.latency_ms:
        await asyncio.sleep(chaos.latency_ms / 1000)
    if chaos.error_percent and random.random() * 100 < chaos.error_percent:
        raise HTTPException(status_code=500, detail="Injected failure")


# ------------------------------------------------------------- middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    IN_PROGRESS.inc()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        IN_PROGRESS.dec()
        route = request.scope.get("route")
        path = route.path if route else "unmatched"  # avoids label explosion
        if path != "/metrics":
            REQUESTS.labels(request.method, path, str(status)).inc()
            LATENCY.labels(request.method, path).observe(time.perf_counter() - start)


# ------------------------------------------------------- health / metrics
@app.get("/health")
def health():
    """Liveness: is the process alive?"""
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response):
    """Readiness: should Kubernetes send traffic to this pod?"""
    if not chaos.ready:
        response.status_code = 503
        return {"status": "not ready"}
    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# -------------------------------------------------------------- business
class OrderIn(BaseModel):
    item: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=1000)


_orders: dict[int, dict] = {}
_ids = count(1)


@app.get("/")
def root():
    return {"service": "orders-api", "version": app.version}


@app.post("/orders", status_code=201, dependencies=[Depends(apply_chaos)])
def create_order(order: OrderIn):
    order_id = next(_ids)
    _orders[order_id] = {"id": order_id, **order.model_dump()}
    ORDERS_CREATED.inc()
    return _orders[order_id]


@app.get("/orders", dependencies=[Depends(apply_chaos)])
def list_orders():
    return list(_orders.values())


@app.get("/orders/{order_id}", dependencies=[Depends(apply_chaos)])
def get_order(order_id: int):
    if order_id not in _orders:
        raise HTTPException(status_code=404, detail="Order not found")
    return _orders[order_id]


# ----------------------------------------------------------------- chaos
@app.get("/chaos/state", dependencies=[Depends(chaos_allowed)])
def chaos_state():
    return {
        "latency_ms": chaos.latency_ms,
        "error_percent": chaos.error_percent,
        "ready": chaos.ready,
    }


@app.post("/chaos/latency", dependencies=[Depends(chaos_allowed)])
def chaos_latency(ms: int = 500):
    chaos.latency_ms = max(0, min(ms, 10000))
    return {"latency_ms": chaos.latency_ms}


@app.post("/chaos/errors", dependencies=[Depends(chaos_allowed)])
def chaos_errors(percent: int = 30):
    chaos.error_percent = max(0, min(percent, 100))
    return {"error_percent": chaos.error_percent}


@app.post("/chaos/cpu", dependencies=[Depends(chaos_allowed)])
def chaos_cpu(seconds: int = 20):
    seconds = max(1, min(seconds, 120))

    def burn():
        end = time.time() + seconds
        while time.time() < end:
            pass

    threading.Thread(target=burn, daemon=True).start()
    return {"cpu_burn_seconds": seconds}


@app.post("/chaos/unready", dependencies=[Depends(chaos_allowed)])
def chaos_unready(seconds: int = 30):
    seconds = max(1, min(seconds, 300))
    chaos.ready = False
    threading.Timer(seconds, lambda: setattr(chaos, "ready", True)).start()
    return {"unready_for_seconds": seconds}


@app.post("/chaos/crash", dependencies=[Depends(chaos_allowed)])
def chaos_crash():
    threading.Timer(0.5, lambda: os._exit(1)).start()
    return {"crashing": True}


@app.post("/chaos/reset", dependencies=[Depends(chaos_allowed)])
def chaos_reset():
    chaos.latency_ms = 0
    chaos.error_percent = 0
    chaos.ready = True
    return {"reset": True}