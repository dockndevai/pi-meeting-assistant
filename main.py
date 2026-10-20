"""
main.py — FastAPI application entry point

Decision: single file for app setup, routes imported as APIRouter
  Keeps the entry point small and readable. Routes live in routes/
  so they can be developed and tested independently.

Decision: one uvicorn worker (set in supervisord.conf)
  This is a single-user meeting assistant, not a public web service.
  Multiple workers would each hold their own in-memory session state,
  causing the dashboard to see a different state than the worker that
  processed the transcript. One worker = one shared memory space,
  zero coordination overhead, simpler on a Pi with 4 cores.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
import uvicorn
import os

load_dotenv()

from routes.transcript import router as transcript_router
from routes.approve    import router as approve_router
from routes.live       import router as live_router
from routes.arm        import router as arm_router


# Decision: lifespan over @app.on_event (on_event is deprecated in FastAPI 0.95+)
@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[server] Meeting assistant starting on port {os.getenv('PORT', '3000')}")
    # Warm Teams + Azure DevOps MCP once (Cursor-style long-lived processes).
    from services.mcp_client import start_mcp_pool, stop_mcp_pool
    from arm.hub import hub as arm_hub
    await arm_hub.start()
    try:
        await start_mcp_pool()
    except Exception as e:
        print(f"[server] MCP pool start warning: {e}")
    yield
    print("[server] Shutting down")
    try:
        await stop_mcp_pool()
    except Exception as e:
        print(f"[server] MCP pool stop warning: {e}")


app = FastAPI(
    title="Pi Meeting Assistant",
    lifespan=lifespan,
    # Decision: disable /docs and /redoc in production — not needed on Pi,
    # saves a small amount of memory. Set docs_url="/docs" to re-enable.
    docs_url=None,
    redoc_url=None,
)

# Decision: temporary raw-body logging on 422s
#   Validation errors happen inside Pydantic, before any route code runs —
#   with --no-access-log in supervisord.conf, a bad request body is otherwise
#   invisible. Logs just enough to diagnose, then can be removed.
@app.exception_handler(RequestValidationError)
async def log_validation_errors(request: Request, exc: RequestValidationError):
    body = await request.body()
    print(f"[validation-error] {request.method} {request.url.path} "
          f"content-type={request.headers.get('content-type')} "
          f"body-len={len(body)} body-preview={body[:300]!r} "
          f"errors={exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(transcript_router, prefix="/api/transcript")
app.include_router(approve_router,    prefix="/api/approve")
app.include_router(live_router)       # /ws/audio, /ws/live, /api/live/*
app.include_router(arm_router)        # /arm, /api/arm/*, /ws/arm, /ws/arm/audio

# ── Static files (dashboard UI) ───────────────────────────────────────────────
# Decision: mount static files before the catch-all route
#   FastAPI resolves routes in registration order. Static mount first,
#   catch-all SPA fallback last — ensures /api/* routes are never swallowed.
app.mount("/static", StaticFiles(directory="public"), name="static")

@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """Serve index.html for all non-API routes (SPA client-side routing)."""
    return FileResponse("public/index.html")


if __name__ == "__main__":
    # Decision: reload=False even in dev — the Pi is the target environment,
    # not a rapid dev loop. Reload adds a file watcher process that wastes
    # CPU on the Pi. Use `docker compose up --build` to pick up changes.
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 3000)),
        reload=False,
        log_level="info",
    )
