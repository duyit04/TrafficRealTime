"""
ML Layer – YOLOv8 Model Wrapper
Encapsulates all direct interactions with the ultralytics YOLO library.
The service layer never imports ultralytics directly.

Supports both:
  - predict(): detection only (no tracking)
  - track(): detection + built-in tracking (ByteTrack / BoT-SORT)
"""

from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
from typing import List
import threading
import re

import numpy as np

from app.core.config import settings
from app.core.logger import logger

# After ByteTrack Kalman failure: fallback to predict() only (no track_id) for this many seconds.
_TRACKER_BROKEN_COOLDOWN_S = 1.0


def is_tracker_kalman_error(exc: BaseException) -> bool:
    """ByteTrack/BoT-SORT Kalman filter can throw when covariance goes singular."""
    if isinstance(exc, np.linalg.LinAlgError):
        return True
    msg = str(exc).lower()
    return (
        "positive definite" in msg
        or "leading minor" in msg
        or "singular matrix" in msg
    )


def is_tracker_state_error(exc: BaseException) -> bool:
    """Tracker internal state corruption — requires tracker reset to recover."""
    if is_tracker_kalman_error(exc):
        return True
    # model.track() returns [] after partial Kalman reset → [][0] = IndexError
    if isinstance(exc, IndexError):
        return True
    msg = str(exc).lower()
    return "list index out of range" in msg or "index out of range" in msg


def _frame_ok_for_track(frame: np.ndarray | None) -> bool:
    if frame is None or not hasattr(frame, "shape"):
        return False
    if frame.size == 0 or frame.ndim < 2:
        return False
    h, w = int(frame.shape[0]), int(frame.shape[1])
    return h >= 32 and w >= 32


def _resolve_yolo_device():
    """
    Pick Ultralytics device + half precision from settings and torch capabilities.
    Returns (device, use_half, label for logs).
    """
    import torch

    mode = (settings.YOLO_DEVICE or "auto").strip().lower()
    cuda_ok = torch.cuda.is_available()
    ver = getattr(torch, "__version__", "?")

    def with_name(dev, half: bool, label: str):
        return dev, half, label

    if mode == "auto":
        if cuda_ok:
            name = torch.cuda.get_device_name(0)
            return with_name(0, True, f"cuda:0 ({name})")
        hint = ""
        if "+cpu" in ver:
            hint = (
                " Install a CUDA build matching your Python version (see "
                "https://pytorch.org/get-started/locally/ ). "
                "Example (Python 3.14 / Windows): --index-url https://download.pytorch.org/whl/cu126"
            )
        logger.warning(
            "YOLOModel: CUDA not available (torch %s). Using CPU.%s", ver, hint
        )
        return with_name("cpu", False, "cpu")

    if mode == "cpu":
        return with_name("cpu", False, "cpu")

    if mode in ("cuda", "gpu", "cuda:0", "0"):
        if cuda_ok:
            name = torch.cuda.get_device_name(0)
            return with_name(0, True, f"cuda:0 ({name})")
        logger.warning(
            "YOLO_DEVICE requests GPU but CUDA is unavailable (torch %s). Using CPU.", ver
        )
        return with_name("cpu", False, "cpu")

    if cuda_ok:
        return with_name(mode, True, mode)
    logger.warning("YOLO_DEVICE=%s but CUDA unavailable; using CPU", mode)
    return with_name("cpu", False, "cpu")


@dataclass
class RawDetection:
    """Raw output from YOLOv8 before any business logic."""
    x1: int
    y1: int
    x2: int
    y2: int
    class_id: int
    class_name: str
    confidence: float
    track_id: int | None = None  # populated when using track()

    @property
    def cx(self) -> int:
        return (self.x1 + self.x2) // 2

    @property
    def cy(self) -> int:
        return (self.y1 + self.y2) // 2


class YOLOModel:
    """
    Thin wrapper around ultralytics YOLO.

    Responsibilities:
    - Load / unload model weights
    - Run inference (predict) or tracking (track) on a single BGR frame
    - Return structured RawDetection list
    """

    def __init__(self) -> None:
        self._model = None
        self._model_path: str = ""
        self._weights_path: Path | None = None
        self._class_names: dict[int, str] = {}
        self._device = "cpu"
        self._use_half = False
        self._runtime_backend: str = "torch"
        self._infer_lock = threading.RLock()
        self._fixed_imgsz_override: int | None = None
        self._tracker_err_log_ts: float = 0.0
        self._tracker_broken_until: float = 0.0

    # ── Public ────────────────────────────────────────────────────────────────

    def load(self, path: str | Path) -> None:
        from ultralytics import YOLO

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Weights not found: {path}")

        self._device, self._use_half, _dev_label = _resolve_yolo_device()

        model, backend, loaded_path = self._load_with_backend_preference(path, YOLO)
        self._model = model
        self._runtime_backend = backend
        self._model_path = loaded_path.name
        self._weights_path = loaded_path.resolve()

        names = getattr(self._model, "names", {})
        if isinstance(names, dict):
            self._class_names = {int(i): str(n) for i, n in names.items()}
        else:
            self._class_names = {i: str(n) for i, n in enumerate(names)}

        logger.info(
            "YOLOModel: loaded %s via %s → device=%s half=%s",
            loaded_path.name,
            backend,
            _dev_label,
            self._use_half,
        )

    def unload(self) -> None:
        self._model = None
        self._model_path = ""
        self._weights_path = None
        self._class_names = {}
        self._runtime_backend = "torch"
        self._fixed_imgsz_override = None

    def load_pretrained(self, name: str = "yolov8n.pt") -> None:
        """
        Load a pretrained Ultralytics model by name (auto-downloads if missing).
        Example: "yolov8n.pt"
        """
        from ultralytics import YOLO

        self._device, self._use_half, _dev_label = _resolve_yolo_device()
        model, backend, loaded_path = self._load_with_backend_preference(Path(str(name)), YOLO)
        self._model = model
        self._runtime_backend = backend
        self._model_path = loaded_path.name
        self._weights_path = loaded_path.resolve()

        names = getattr(self._model, "names", {})
        if isinstance(names, dict):
            self._class_names = {int(i): str(n) for i, n in names.items()}
        else:
            self._class_names = {i: str(n) for i, n in enumerate(names)}

        logger.info(
            "YOLOModel: loaded pretrained %s via %s (%s) → device=%s half=%s",
            str(name),
            backend,
            loaded_path.name,
            _dev_label,
            self._use_half,
        )

    def predict(self, frame: np.ndarray, conf: float = 0.35) -> List[RawDetection]:
        """Run inference on a BGR numpy frame (detection only, no tracking)."""
        if not self.is_loaded:
            return []

        with self._infer_lock:
            kwargs = self._runtime_infer_kwargs()
            imgsz = self._effective_imgsz()
            try:
                results = self._model(frame, verbose=False, conf=conf, imgsz=imgsz, **kwargs)[0]
            except Exception as e:
                retry_imgsz = self._extract_engine_max_imgsz(e)
                if retry_imgsz is None or retry_imgsz == imgsz:
                    raise
                self._fixed_imgsz_override = retry_imgsz
                logger.warning(
                    "YOLOModel: imgsz %s incompatible with engine, retry predict at %s",
                    imgsz,
                    retry_imgsz,
                )
                results = self._model(frame, verbose=False, conf=conf, imgsz=retry_imgsz, **kwargs)[0]
            return self._parse_boxes(results)

    def track(
        self,
        frame: np.ndarray,
        conf: float = 0.35,
        tracker: str = "bytetrack.yaml",
        persist: bool = True,
    ) -> List[RawDetection]:
        """
        Run inference + built-in tracking (ByteTrack or BoT-SORT).

        Args:
            frame: BGR numpy array
            conf: confidence threshold
            tracker: "bytetrack.yaml" or "botsort.yaml"
            persist: keep track IDs across frames (must be True for continuous tracking)

        Returns:
            List of RawDetection with track_id populated
        """
        import time as _time

        if not self.is_loaded:
            return []
        if not _frame_ok_for_track(frame):
            return []

        # During cooldown after tracker failure, fall back to predict() to avoid per-frame overhead
        now = _time.monotonic()
        if now < self._tracker_broken_until:
            return self.predict(frame, conf)

        with self._infer_lock:
            kwargs = self._runtime_infer_kwargs()
            imgsz = self._effective_imgsz()

            def _run_track(imgsz_val: int):
                result_list = self._model.track(
                    frame,
                    verbose=False,
                    conf=conf,
                    tracker=tracker,
                    persist=persist,
                    imgsz=imgsz_val,
                    **kwargs,
                )
                if not result_list:
                    raise IndexError("model.track() returned empty list")
                return result_list[0]

            try:
                results = _run_track(imgsz)
                # Successful run — clear any broken state
                self._tracker_broken_until = 0.0
            except Exception as e:
                if is_tracker_state_error(e):
                    now2 = _time.monotonic()
                    if now2 - self._tracker_err_log_ts > 5.0:
                        self._tracker_err_log_ts = now2
                        logger.warning(
                            "YOLOModel: tracker state error (%s), resetting — will use predict() for %.1fs",
                            e,
                            _TRACKER_BROKEN_COOLDOWN_S,
                        )
                    self.reset_tracker()
                    self._tracker_broken_until = _time.monotonic() + _TRACKER_BROKEN_COOLDOWN_S
                    return self.predict(frame, conf)
                else:
                    retry_imgsz = self._extract_engine_max_imgsz(e)
                    if retry_imgsz is None or retry_imgsz == imgsz:
                        raise
                    self._fixed_imgsz_override = retry_imgsz
                    logger.warning(
                        "YOLOModel: imgsz %s incompatible with engine, retry track at %s",
                        imgsz,
                        retry_imgsz,
                    )
                    results = _run_track(retry_imgsz)
            return self._parse_boxes(results)

    def reset_tracker(self) -> None:
        """Reset tracker stracks without destroying the tracker list (avoids IndexError on next call)."""
        if self._model is None:
            return
        predictor = getattr(self._model, "predictor", None)
        if predictor is None:
            return
        trackers = getattr(predictor, "trackers", None)
        if not trackers:
            return
        for t in trackers:
            try:
                for attr in ("tracked_stracks", "lost_stracks", "removed_stracks"):
                    if hasattr(t, attr):
                        setattr(t, attr, [])
                if hasattr(t, "frame_id"):
                    t.frame_id = 0
            except Exception:
                pass

    def export_engine(
        self,
        path: str | Path,
        *,
        fp16: bool | None = None,
        workspace_gb: int | None = None,
        imgsz: int | None = None,
    ) -> Path:
        """
        Export a .pt model to TensorRT .engine and return output path.
        """
        from ultralytics import YOLO

        src = Path(path)
        if src.suffix.lower() != ".pt":
            raise ValueError("TensorRT export requires a .pt model file")
        if not src.exists():
            raise FileNotFoundError(f"Model not found: {src}")

        if self._device == "cpu":
            raise RuntimeError("CUDA is required for TensorRT export")

        use_fp16 = bool(getattr(settings, "YOLO_TRT_FP16", True) if fp16 is None else fp16)
        workspace = int(getattr(settings, "YOLO_TRT_WORKSPACE_GB", 4) if workspace_gb is None else workspace_gb)
        export_imgsz = int(getattr(settings, "YOLO_IMGSZ", 640) if imgsz is None else imgsz)

        model = YOLO(str(src))
        export_kwargs = {
            "format": "engine",
            "device": 0,
            "imgsz": export_imgsz,
            "half": use_fp16,
        }
        if workspace > 0:
            export_kwargs["workspace"] = workspace

        exported = model.export(**export_kwargs)
        out = Path(str(exported)) if exported else src.with_suffix(".engine")
        if not out.exists():
            raise RuntimeError("TensorRT export finished but .engine file not found")

        # Xóa file .onnx trung gian mà Ultralytics để lại sau khi build TensorRT
        onnx_artifact = src.with_suffix(".onnx")
        if onnx_artifact.exists():
            try:
                onnx_artifact.unlink()
                logger.info("YOLOModel: removed intermediate ONNX artifact %s", onnx_artifact.name)
            except Exception as e:
                logger.warning("YOLOModel: could not remove ONNX artifact: %s", e)

        return out

    # ── Private ───────────────────────────────────────────────────────────────

    def _parse_boxes(self, results) -> List[RawDetection]:
        """Parse ultralytics Results into RawDetection list."""
        detections: List[RawDetection] = []

        if results.boxes is None:
            return detections

        fh = int(getattr(results, "orig_shape", (0, 0))[0] or 0)
        fw = int(getattr(results, "orig_shape", (0, 0))[1] or 0)

        for box in results.boxes:
            cls_id = int(box.cls[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            if x2 <= x1 or y2 <= y1:
                continue
            if (x2 - x1) * (y2 - y1) < 4:
                continue
            if fw > 0 and fh > 0:
                if x1 < -2 or y1 < -2 or x2 > fw + 2 or y2 > fh + 2:
                    continue

            # track_id is available when using model.track()
            tid = None
            if box.id is not None:
                tid = int(box.id[0])

            detections.append(
                RawDetection(
                    x1=x1, y1=y1, x2=x2, y2=y2,
                    class_id=cls_id,
                    class_name=self._class_names.get(cls_id, str(cls_id)),
                    confidence=round(float(box.conf[0]), 3),
                    track_id=tid,
                )
            )

        return detections

    def _runtime_infer_kwargs(self) -> dict:
        if self._runtime_backend == "tensorrt":
            # TensorRT runtime already binds execution device/precision.
            return {}
        return {"device": self._device, "half": self._use_half}

    def _effective_imgsz(self) -> int:
        """Preferred inference size; can be overridden when static TensorRT engine requires a fixed size."""
        if self._fixed_imgsz_override is not None:
            return int(self._fixed_imgsz_override)
        return int(getattr(settings, "YOLO_IMGSZ", 640) or 640)

    @staticmethod
    def _extract_engine_max_imgsz(err: Exception) -> int | None:
        """
        Parse TensorRT static-shape mismatch errors like:
          "input size torch.Size([1, 3, 320, 320]) not equal to max model size (1, 3, 416, 416)"
        and return 416.
        """
        msg = str(err)
        m = re.search(r"max model size\s*\(\s*1\s*,\s*3\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", msg)
        if not m:
            return None
        h = int(m.group(1))
        w = int(m.group(2))
        return h if h == w else max(h, w)

    def _load_with_backend_preference(self, src_path: Path, yolo_cls):
        """
        Load model with backend preference:
          - TensorRT (engine) when requested and CUDA is available
          - fallback to PyTorch (.pt)
        """
        backend_pref = str(getattr(settings, "YOLO_BACKEND", "auto") or "auto").strip().lower()
        backend_pref = backend_pref if backend_pref in {"auto", "torch", "tensorrt"} else "auto"
        cuda_enabled = self._device != "cpu"

        want_trt = backend_pref in {"auto", "tensorrt"} and cuda_enabled
        src = Path(src_path)
        engine_path = src.with_suffix(".engine")

        if want_trt:
            if engine_path.exists():
                try:
                    return yolo_cls(str(engine_path)), "tensorrt", engine_path
                except Exception as e:
                    logger.warning("YOLOModel: load TensorRT sidecar failed (%s). Fallback to torch.", e)

            auto_export = bool(getattr(settings, "YOLO_TRT_AUTO_EXPORT", False))
            if auto_export and src.suffix.lower() == ".pt":
                try:
                    base = yolo_cls(str(src))
                    export_kwargs = {
                        "format": "engine",
                        "device": 0,
                        "imgsz": int(getattr(settings, "YOLO_IMGSZ", 640) or 640),
                        "half": bool(getattr(settings, "YOLO_TRT_FP16", True)),
                    }
                    workspace = int(getattr(settings, "YOLO_TRT_WORKSPACE_GB", 4) or 4)
                    if workspace > 0:
                        export_kwargs["workspace"] = workspace
                    exported = base.export(**export_kwargs)
                    out_path = Path(str(exported)) if exported else engine_path
                    if out_path.exists():
                        return yolo_cls(str(out_path)), "tensorrt", out_path
                    logger.warning("YOLOModel: TensorRT export done but engine not found, fallback torch.")
                except Exception as e:
                    logger.warning("YOLOModel: TensorRT export failed (%s). Fallback to torch.", e)

        # Default torch path
        return yolo_cls(str(src)), "torch", src

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def runtime_backend(self) -> str:
        return self._runtime_backend

    @property
    def runtime_device(self):
        return self._device

    @property
    def runtime_half(self) -> bool:
        return bool(self._use_half)

    @property
    def weights_path(self) -> Path | None:
        """
        Full path to the currently loaded weights (only for local .pt loads).
        Returns None when using load_pretrained().
        """
        return self._weights_path


# Module-level singleton
yolo_model = YOLOModel()
