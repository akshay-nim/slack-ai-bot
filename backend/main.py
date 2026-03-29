# main.py
import os
import uvicorn
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

from backend.config import KUBECONFIG_MAP
from backend.config import LOG_LEVEL
from backend.kube_client import KubeClientManager, LogFetcher
from backend.handlers import PodCrashLoopingHandler

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("backend")

# ── FastAPI & Metrics ─────────────────────────────────────────────
app = FastAPI(title="Alert Log Fetcher + LLM Summarizer")
REQUESTS = Counter("fetch_requests_total", "Total fetch requests", ["alertname"])
LATENCY  = Histogram("fetch_latency_seconds", "Latency by alertname", ["alertname"])

# ── Pydantic input model ───────────────────────────────────────────
class FetchRequest(BaseModel):
    alertname:  str
    cluster:    str
    namespace:  str
    pod:        str
    container:  str | None = None
    tail_lines: int | None = None

# ── Handler registry ──────────────────────────────────────────────
manager = KubeClientManager()
fetcher = LogFetcher(manager)
HANDLERS = {
    "PodCrashLooping": PodCrashLoopingHandler(fetcher),
    "PodInErrorState": PodCrashLoopingHandler(fetcher),
    "PodPendingTooLong": PodCrashLoopingHandler(fetcher)
    # add more to support other alert types
}

# ── Routes ────────────────────────────────────────────────────────
@app.post("/fetch-logs")
async def fetch_logs(req: FetchRequest):
    logger.info(f"Received request: {req}")
    if req.alertname not in HANDLERS:
        raise HTTPException(400, f"No handler for {req.alertname}")
    
    logger.info(f"Using handler: {HANDLERS[req.alertname].__class__.__name__}")
    handler = HANDLERS[req.alertname]
    REQUESTS.labels(req.alertname).inc()
    with LATENCY.labels(req.alertname).time():
        result = await handler.handle(req.model_dump())

    if "error" in result:
        raise HTTPException(400, result["error"])
    return result

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/readyz")
async def readyz():
    ok = bool(KUBECONFIG_MAP)
    return ({"ready": ok}, 200 if ok else 500)

@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(data, media_type=CONTENT_TYPE_LATEST)


# ── Run with Uvicorn ─────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=5000,
        reload=True
    )
