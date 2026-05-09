"""
Model Service – manages YOLO model lifecycle.
Delegates all ML work to the YOLOModel in the ml/ layer.
"""

from __future__ import annotations
from pathlib import Path

from app.core.config import settings
from app.core.logger import logger
from app.ml.yolo_model import YOLOModel, yolo_model
from app.models.detection_model import ModelInfo


class ModelService:
    """
    Service layer: model file management (upload path, list, delete).

    Does NOT contain any ML code – all inference is in app.ml.yolo_model.
    """

    def __init__(self, model: YOLOModel) -> None:
        # Inject the ML model (allows easy test mocking)
        self._yolo = model

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, path: Path | str) -> None:
        path = Path(path)
        logger.info("ModelService: load %s", path.name)
        self._yolo.load(path)

    def unload(self) -> None:
        self._yolo.unload()
        logger.info("ModelService: model unloaded")

    def delete(self, name: str) -> None:
        p = self._resolve(name)
        if not p.exists():
            raise FileNotFoundError(f"Model '{name}' not found")
        if name == self._yolo.model_path:
            self.unload()
        p.unlink()
        logger.info("ModelService: deleted %s", name)

    def list_models(self) -> list[ModelInfo]:
        """
        List all available .pt/.engine models.

        - Primary location: under MODELS_DIR (supports nested folders, e.g. yolov8/best.pt)
        - Legacy: root of UPLOADS_DIR
        """
        results: list[ModelInfo] = []

        # Any .pt/.engine under MODELS_DIR (including yolov8/, yolov3/, yolov26/)
        model_files = list(settings.MODELS_DIR.glob("**/*.pt")) + list(settings.MODELS_DIR.glob("**/*.engine"))
        for f in model_files:
            try:
                rel = f.relative_to(settings.MODELS_DIR).as_posix()
            except ValueError:
                rel = f.name
            results.append(
                ModelInfo(
                    name=rel,
                    size_mb=round(f.stat().st_size / 1e6, 2),
                    active=(rel == self._yolo.model_path),
                )
            )

        # Legacy: uploads root
        legacy_files = list(settings.UPLOADS_DIR.glob("*.pt")) + list(settings.UPLOADS_DIR.glob("*.engine"))
        for f in legacy_files:
            results.append(
                ModelInfo(
                    name=f.name,
                    size_mb=round(f.stat().st_size / 1e6, 2),
                    active=(f.name == self._yolo.model_path),
                )
            )

        # Remove potential duplicates (same logical name)
        dedup: dict[str, ModelInfo] = {m.name: m for m in results}
        return sorted(dedup.values(), key=lambda x: x.name)

    def resolve(self, name: str) -> Path:
        return self._resolve(name)

    def export_engine(
        self,
        name: str,
        *,
        fp16: bool | None = None,
        workspace_gb: int | None = None,
        imgsz: int | None = None,
        load_after_export: bool = True,
    ) -> Path:
        src = self._resolve(name)
        if not src.exists():
            raise FileNotFoundError(f"Model '{name}' not found")
        engine_path = self._yolo.export_engine(
            src,
            fp16=fp16,
            workspace_gb=workspace_gb,
            imgsz=imgsz,
        )
        logger.info("ModelService: exported TensorRT engine %s", engine_path.name)
        if load_after_export:
            self.load(engine_path)
        return engine_path

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._yolo.is_loaded

    @property
    def name(self) -> str:
        return self._yolo.model_path

    # ── Internals ─────────────────────────────────────────────────────────────

    def _resolve(self, name: str) -> Path:
        """
        Resolve a logical model name to a filesystem path.

        - If name contains a slash, treat it as relative to MODELS_DIR (e.g. yolov8/best.pt)
        - Otherwise, search:
            1) root of MODELS_DIR
            2) immediate subfolders of MODELS_DIR
            3) root of UPLOADS_DIR
        """
        # Name with nested folder (e.g. "yolov8/best.pt")
        if "/" in name or "\\" in name:
            p = (settings.MODELS_DIR / name).resolve()
            return p

        # 1) Root of MODELS_DIR
        root_candidate = settings.MODELS_DIR / name
        if root_candidate.exists():
            return root_candidate

        # 2) First-level subfolders under MODELS_DIR
        for sub in settings.MODELS_DIR.iterdir():
            if sub.is_dir():
                candidate = sub / name
                if candidate.exists():
                    return candidate

        # 3) Root of UPLOADS_DIR
        legacy = settings.UPLOADS_DIR / name
        if legacy.exists():
            return legacy

        # Fallback: assume under MODELS_DIR
        return settings.MODELS_DIR / name


# Singleton – inject the shared YOLOModel instance from ml layer
model_service = ModelService(model=yolo_model)
