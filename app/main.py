"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI

from .routers import router
from .routers_sandbox import router as sandbox_router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "A read-only sandbox evaluates draft anomaly rules against pinned "
        "events without touching production cases or notifications."
    ),
)

app.include_router(router)
app.include_router(sandbox_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
