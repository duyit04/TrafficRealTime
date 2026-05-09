"""
FastAPI Application Entry Point
Traffic Monitor – YOLOv8 Vehicle Detection & Counting API
"""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logger import logger
from app.api import model_routes, stream_routes, detection_routes, traffic_light_routes
from app.api.ws_routes import router as ws_router
from app.services.model_service import model_service
from app.services.stream_service import stream_service
from app.ml.tracker import get_tracker


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("═══ Traffic Monitor API starting on :%d ═══", settings.PORT)

    # Store the running event loop so StreamService worker thread can broadcast via WebSocket
    stream_service.set_event_loop(asyncio.get_event_loop())
    logger.info("Docs: http://localhost:%d/docs", settings.PORT)

    # Validate tracker type at startup
    tracker_type = (settings.TRACKER_TYPE or "bytetrack").strip().lower()
    if tracker_type not in ("bytetrack", "botsort"):
        raise RuntimeError("Invalid TRACKER_TYPE. Use 'bytetrack' or 'botsort'.")
    get_tracker(tracker_type)
    logger.info("Tracker ready: %s (ultralytics built-in)", tracker_type)

    # Auto-load a default model so UI is ready on first open.
    # Priority:
    # 1) DEFAULT_MODEL if it exists (e.g. best.engine / best.pt)
    # 2) Any **/best.engine under models_storage
    # 3) Any **/best.pt under models_storage
    # 4) First .engine found under models_storage
    # 5) First .pt found under models_storage
    # 6) Fallback to pretrained yolov8n.pt
    try:
        chosen = None

        if settings.DEFAULT_MODEL:
            candidate = settings.MODELS_DIR / settings.DEFAULT_MODEL
            if candidate.exists():
                chosen = candidate

        if chosen is None:
            best_engines = sorted(settings.MODELS_DIR.glob("**/best.engine"))
            if best_engines:
                chosen = best_engines[0]

        if chosen is None:
            bests = sorted(settings.MODELS_DIR.glob("**/best.pt"))
            if bests:
                chosen = bests[0]

        if chosen is None:
            engines = sorted(settings.MODELS_DIR.glob("**/*.engine"))
            if engines:
                chosen = engines[0]

        if chosen is None:
            pts = sorted(settings.MODELS_DIR.glob("**/*.pt"))
            if pts:
                chosen = pts[0]

        if chosen is not None and chosen.exists():
            model_service.load(chosen)
            logger.info("Default model loaded: %s", chosen)
        else:
            from app.ml.yolo_model import yolo_model
            yolo_model.load_pretrained("yolov8n.pt")
            logger.info("Default model loaded: yolov8n.pt (pretrained)")
    except Exception as e:
        logger.warning("Could not auto-load default model: %s", e)

    yield
    logger.info("Traffic Monitor API shutting down")


app = FastAPI(
    title=settings.APP_NAME,
    description="YOLOv8 Vehicle Detection & Counting – Demo API",
    version="2.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(model_routes.router)
app.include_router(stream_routes.router)
app.include_router(detection_routes.router)
app.include_router(traffic_light_routes.router)
app.include_router(ws_router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/", tags=["system"])
async def root():
    return {
        "app": settings.APP_NAME,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
