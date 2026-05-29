"""
Stream Service – video capture thread + full detection pipeline.

Pipeline per frame:
  OpenCV → YOLOModel.track() (built-in ByteTrack) → VehicleCounter → CongestionMonitor
"""

from __future__ import annotations
import asyncio
import json
import threading
import time
from collections import deque
from queue import Queue, Empty, Full

import cv2
import numpy as np

from app.core.config import settings
from app.core.logger import logger
from app.models.detection_model import VehicleStats, CongestionInfo

# ── ML Layer ──────────────────────────────────────────────────────────────────
from app.ml.yolo_model import yolo_model, is_tracker_kalman_error, is_tracker_state_error
from app.ml.tracker import get_tracker
from app.ml.vehicle_counter import VehicleCounter
from app.ml.congestion_monitor import CongestionMonitor

# ── Service Layer ─────────────────────────────────────────────────────────────
from app.services.roi_service import roi_service
from app.services.ffmpeg_relay_service import (
    ffmpeg_relay_service,
    ffmpeg_relay_companion_service,
    ffmpeg_relay_extra2_service,
    ffmpeg_relay_extra3_service,
    h264_bgr_primary,
    h264_bgr_companion,
    h264_bgr_extra2,
    h264_bgr_extra3,
    should_h264_burnin,
    use_h264_rtsp_relay,
)
from app.utils.frame_overlay import overlay_traffic_ui
from app.utils.image_utils import resize_bgr_max_width
from app.utils.video_utils import is_youtube_url, resolve_youtube_url, validate_youtube_url
from app.api.ws_routes import ws_manager, ws_companion_manager, stats_message_json


class StreamService:
    """
    Manages video stream lifecycle.

    Pipeline: YOLOModel.track() → BuiltinTracker (convert to Track) → VehicleCounter → CongestionMonitor
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        self._latest: dict | None = None

        # Runtime settings
        self.conf_threshold: float = settings.CONF_THRESHOLD
        self.line_position: float = settings.LINE_POSITION
        self.max_fps: int = settings.MAX_FPS
        self._tracker_type: str = settings.TRACKER_TYPE
        # Stream quality/perf knobs (runtime patchable via /api/v1/detection/settings)
        self.jpeg_quality: int = int(getattr(settings, "STREAM_JPEG_QUALITY", 75) or 75)
        self.max_width: int = int(getattr(settings, "STREAM_MAX_WIDTH", 0) or 0)
        self.skip_frames: int = int(getattr(settings, "INFERENCE_SKIP_FRAMES", 0) or 0)

        # ML sub-components
        self._tracker = get_tracker(self._tracker_type)
        self._counter = VehicleCounter()
        self._congestion = CongestionMonitor(
            vehicle_threshold=settings.CONGESTION_VEHICLE_THRESHOLD,
            stable_duration=settings.CONGESTION_STABLE_DURATION,
        )
        self._companion_tracker = get_tracker(self._tracker_type)
        self._companion_counter = VehicleCounter()
        self._companion_congestion = CongestionMonitor(
            vehicle_threshold=settings.CONGESTION_VEHICLE_THRESHOLD,
            stable_duration=settings.CONGESTION_STABLE_DURATION,
        )
        # Per-camera YOLO instances for independent tracking state.
        # (Ultralytics built-in trackers store state inside the model predictor.)
        from app.ml.yolo_model import YOLOModel

        self._companion_yolo = YOLOModel()
        self._extra_yolo: dict[int, YOLOModel] = {}

        # Runtime state
        self._stats = VehicleStats()
        self._counting_line_y: int | None = None
        self._timeline: list[dict] = []
        self._timeline_last: int = 0
        self._last_error: str | None = None
        # Legacy: stored event loop for optional WS broadcasting (may be unused)
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # Recent HTTP polling activity marker.
        # If inactive, skip expensive base64 encode and rely on WebSocket bytes.
        self._http_primary_last_poll_ts: float = 0.0
        self._http_companion_last_poll_ts: float = 0.0
        self._http_extra_last_poll_ts: dict[int, float] = {}
        # For inference skipping (keep last detections to reuse on skipped frames)
        self._skip_counter: int = 0
        self._last_api_dets: list[dict] = []
        self._pipe_err_log_ts: float = 0.0
        # Stop detection inside ROI (per slot) for traffic-light inference
        self._stop_hist: dict[str, dict[int, dict]] = {
            "primary": {},
            "companion": {},
            "2": {},
            "3": {},
        }
        # Optional second RTSP — same intersection, other approach (video only)
        self._companion_thread: threading.Thread | None = None
        self._companion_running: bool = False
        self._companion_url: str = ""
        self._companion_latest: dict | None = None
        self._companion_lock = threading.Lock()
        self._companion_frame_count: int = 0
        self._companion_fps: float = 0.0
        self._companion_counting_line_y: int | None = None

        # Extra slots: per-slot tracker + counter (gộp vào GET /stats)
        self._extra_trackers: dict[int, object] = {}
        self._extra_counters: dict[int, VehicleCounter] = {}
        self._extra_counting_line_y: dict[int, int] = {}
        self._merge_lock = threading.RLock()

        # Extra live streams (screens 3/4): each has its own worker + latest payload
        self._extra_threads: dict[int, threading.Thread] = {}
        self._extra_running: dict[int, bool] = {}
        self._extra_urls: dict[int, str] = {}
        self._extra_latest: dict[int, dict] = {}
        self._extra_lock = threading.Lock()

        # Source mode: "rtsp" | "file"
        self._source_mode: str = "rtsp"

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def _drain_latest_frame(frame_q: Queue, timeout: float = 1.0) -> tuple[np.ndarray | None, int]:
        """Take the newest frame; drop older queued frames when the worker falls behind."""
        try:
            frame = frame_q.get(timeout=timeout)
        except Empty:
            return None, 0
        dropped = 0
        while True:
            try:
                frame = frame_q.get_nowait()
                dropped += 1
            except Empty:
                break
        return frame, dropped

    @staticmethod
    def _h264_relay_for_slot(slot: int | str):
        s = str(slot).strip().lower()
        if s in {"companion", "cam2"}:
            return ffmpeg_relay_companion_service
        if s in {"2", "extra2", "cam3"}:
            return ffmpeg_relay_extra2_service
        if s in {"3", "extra3", "cam4"}:
            return ffmpeg_relay_extra3_service
        return ffmpeg_relay_service

    @staticmethod
    def _h264_bgr_for_slot(slot: int | str):
        s = str(slot).strip().lower()
        if s in {"2", "extra2", "cam3"}:
            return h264_bgr_extra2
        if s in {"3", "extra3", "cam4"}:
            return h264_bgr_extra3
        if s in {"companion", "cam2"}:
            return h264_bgr_companion
        return h264_bgr_primary

    @classmethod
    def _start_h264_relay(cls, slot: int | str, url: str) -> None:
        if not use_h264_rtsp_relay():
            return
        u = (url or "").strip()
        if not u.lower().startswith("rtsp://"):
            return
        try:
            cls._h264_relay_for_slot(slot).start(u)
        except Exception as re:
            logger.debug("H264 RTSP relay start skipped (%s): %s", slot, re)

    @classmethod
    def _stop_h264_slot(cls, slot: int | str) -> None:
        try:
            cls._h264_relay_for_slot(slot).stop()
        except Exception:
            pass
        try:
            cls._h264_bgr_for_slot(slot).stop()
        except Exception:
            pass

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """
        Store the running asyncio loop for integrations that need to schedule work
        from the worker thread (e.g., WebSocket broadcasts).

        Safe to call even if the StreamService does not currently use it.
        """
        self._event_loop = loop

    def start(self, url: str, companion_url: str | None = None) -> None:
        self._last_error = None
        self._stats.stream_error = ""
        url = url.strip()
        cu = (companion_url or "").strip()
        t = threading.Thread(target=self._do_start_with_stop, args=(url, cu), daemon=True)
        t.start()
        logger.info("StreamService: start requested → %s", url[:80])

    def start_companion(self, url: str) -> None:
        """
        Start (or replace) companion stream without restarting primary.
        Requires primary to be running.
        """
        u = (url or "").strip()
        if not u:
            raise ValueError("url is required")
        if not self._running:
            raise RuntimeError("Primary stream is not running")
        # Stop existing companion if any
        self._companion_running = False
        if self._companion_thread:
            self._companion_thread.join(timeout=3)
            self._companion_thread = None
        self._companion_url = u
        self._companion_running = True
        self._companion_thread = threading.Thread(
            target=self._companion_worker,
            args=(self._companion_url,),
            daemon=True,
        )
        self._companion_thread.start()
        self._start_h264_relay("companion", self._companion_url)
        logger.info("StreamService: companion started (no restart) → %s", self._companion_url[:80])

    def stop_companion(self) -> None:
        """Stop companion stream without stopping primary."""
        self._companion_running = False
        self._stop_h264_slot("companion")
        if self._companion_thread:
            self._companion_thread.join(timeout=3)
            self._companion_thread = None
        self._companion_url = ""
        with self._companion_lock:
            self._companion_latest = None
        logger.info("StreamService: companion stopped (no restart)")

    def start_extra(self, slot: int, url: str) -> None:
        """Start an extra live stream (slot=2 for screen 3, slot=3 for screen 4)."""
        s = int(slot)
        if s < 2 or s > 3:
            raise ValueError("extra slot must be 2 or 3")
        u = (url or "").strip()
        if not u:
            raise ValueError("url is required")
        with self._extra_lock:
            self._extra_urls[s] = u
            self._extra_running[s] = True
        t = threading.Thread(target=self._extra_worker, args=(s, u), daemon=True)
        self._extra_threads[s] = t
        t.start()
        self._start_h264_relay(s, u)

    def stop_extra(self, slot: int) -> None:
        s = int(slot)
        self._stop_h264_slot(s)
        with self._extra_lock:
            self._extra_running[s] = False
            self._extra_urls.pop(s, None)
            self._extra_latest.pop(s, None)

    def get_extra_latest(self, slot: int) -> dict | None:
        s = int(slot)
        with self._extra_lock:
            return self._extra_latest.get(s)

    def _do_start_with_stop(self, url: str, companion_url: str = "") -> None:
        self.stop()
        self._do_start(url, companion_url)

    def _do_start(self, url: str, companion_url: str = "") -> None:
        try:
            self._reset_all()
            effective_url = url
            if is_youtube_url(effective_url):
                validate_youtube_url(effective_url)
                resolved = resolve_youtube_url(effective_url)
                if not resolved:
                    self._last_error = "Không thể lấy stream từ link YouTube."
                    self._stats.stream_error = self._last_error
                    logger.warning("StreamService: %s", self._last_error)
                    return
                effective_url = resolved
                logger.info("StreamService: YouTube resolved")
            self._running = True
            self._companion_url = companion_url.strip()
            if self._companion_url:
                self._companion_running = True
                self._companion_thread = threading.Thread(
                    target=self._companion_worker,
                    args=(self._companion_url,),
                    daemon=True,
                )
                self._companion_thread.start()
                self._start_h264_relay("companion", self._companion_url)
                logger.info("StreamService: companion TLC lane → %s", self._companion_url[:80])
            self._thread = threading.Thread(target=self._worker, args=(effective_url,), daemon=True)
            self._thread.start()
            self._start_h264_relay("primary", effective_url)
            logger.info("StreamService: worker started")
        except Exception as e:
            self._last_error = str(e)
            self._stats.stream_error = str(e)
            self._running = False
            logger.exception("StreamService: start failed: %s", e)

    def stop(self) -> None:
        self._running = False
        self._companion_running = False
        for slot in ("primary", "companion", 2, 3):
            self._stop_h264_slot(slot)
        # Stop extra streams
        with self._extra_lock:
            for k in list(self._extra_running.keys()):
                self._extra_running[k] = False
            self._extra_latest.clear()
            self._extra_urls.clear()
        if self._companion_thread:
            self._companion_thread.join(timeout=5)
            self._companion_thread = None
        self._companion_url = ""
        if self._thread:
            self._thread.join(timeout=4)
            self._thread = None
        with self._lock:
            self._latest = None
        with self._companion_lock:
            self._companion_latest = None
        self._stats.stream_active = False
        self._last_error = None
        self._stats.stream_error = ""
        try:
            from app.services.traffic_light_service import traffic_light_service as tls

            tls.set_stream_live(False)
        except Exception as e:
            logger.debug("TrafficLight detach(stop): %s", e)
        logger.info("StreamService: stopped")

    def reset_counters(self) -> None:
        self._counter.reset()
        self._tracker.reset()
        self._companion_counter.reset()
        self._companion_tracker.reset()
        yolo_model.reset_tracker()
        try:
            self._companion_yolo.reset_tracker()
        except Exception:
            pass
        for m in list(self._extra_yolo.values()):
            try:
                m.reset_tracker()
            except Exception:
                pass
        for tr in self._extra_trackers.values():
            try:
                tr.reset()
            except Exception:
                pass
        for c in self._extra_counters.values():
            c.reset()
        self._congestion.reset()
        self._companion_congestion.reset()
        self._stats.total = 0
        self._stats.count_in = 0
        self._stats.count_out = 0
        self._stats.classes = {}
        self._stats.classes_in = {}
        self._stats.classes_out = {}
        self._stats.congestion = CongestionInfo()
        self._timeline.clear()
        self._timeline_last = 0
        for v in self._stop_hist.values():
            v.clear()
        logger.info("StreamService: counters reset")

    def update_settings(
        self,
        conf: float | None = None,
        line: float | None = None,
        fps: int | None = None,
        tracker_type: str | None = None,
        counting_mode: str | None = None,
        congestion_threshold: int | None = None,
        congestion_duration: float | None = None,
        jpeg_quality: int | None = None,
        max_width: int | None = None,
        skip_frames: int | None = None,
    ) -> None:
        if conf is not None:
            self.conf_threshold = max(0.1, min(0.95, conf))
        if line is not None:
            self.line_position = max(0.05, min(0.95, line))
            self._counting_line_y = None
            self._companion_counting_line_y = None
            self._extra_counting_line_y.clear()
        if fps is not None:
            self.max_fps = max(1, min(60, fps))
        if jpeg_quality is not None:
            self.jpeg_quality = max(30, min(95, int(jpeg_quality)))
        if max_width is not None:
            self.max_width = max(0, min(1920, int(max_width)))
        if skip_frames is not None:
            self.skip_frames = max(0, min(10, int(skip_frames)))
        if tracker_type is not None:
            t = str(tracker_type).strip().lower()
            if t in ("bytetrack", "botsort", "sort", "deepsort"):
                self._tracker_type = t
                self._tracker = get_tracker(t)
                self._companion_tracker = get_tracker(t)
                self._extra_trackers.clear()
                yolo_model.reset_tracker()
        if counting_mode is not None:
            self._counter.set_mode(counting_mode)
            self._companion_counter.set_mode(counting_mode)
            for c in self._extra_counters.values():
                c.set_mode(counting_mode)
        self._congestion.update_settings(
            threshold=congestion_threshold,
            duration=congestion_duration,
        )

    def get_latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def get_companion_latest(self) -> dict | None:
        with self._companion_lock:
            return self._companion_latest

    def get_extra_latest(self, slot: int) -> dict | None:
        with self._extra_lock:
            return self._extra_latest.get(int(slot))

    def streams_status(self) -> dict:
        """
        Return lightweight runtime status for all live pipelines.
        Used by UI / ops to know how many streams are running.
        """
        with self._extra_lock:
            extra = {
                int(k): {
                    "running": bool(self._extra_running.get(int(k), False)),
                    "url": str(self._extra_urls.get(int(k), "")),
                }
                for k in sorted(self._extra_urls.keys())
            }
        return {
            "primary": {"running": bool(self._running)},
            "companion": {"running": bool(self._companion_running), "url": str(self._companion_url or "")},
            "extra": extra,
        }

    def mark_http_poll(self, channel: str, slot: int | None = None) -> None:
        """Mark a recent HTTP polling access for frame fallback endpoints."""
        now = time.monotonic()
        ch = str(channel).strip().lower()
        if ch == "primary":
            self._http_primary_last_poll_ts = now
            return
        if ch == "companion":
            self._http_companion_last_poll_ts = now
            return
        if ch == "extra" and slot is not None:
            self._http_extra_last_poll_ts[int(slot)] = now

    @staticmethod
    def _is_http_poll_active(last_poll_ts: float, ttl_s: float = 2.0) -> bool:
        return (time.monotonic() - float(last_poll_ts)) <= float(ttl_s)

    def _sync_secondary_model(self, local_model) -> bool:
        """
        Ensure a secondary YOLOModel has the same weights as the primary yolo_model.
        Returns True if ready to run tracking; False if no primary model is loaded.
        """
        try:
            if not yolo_model.is_loaded:
                return False
            wp = getattr(yolo_model, "weights_path", None)
            if wp is None:
                return False
            # Reload if not loaded or model name changed.
            if (not local_model.is_loaded) or (local_model.model_path != yolo_model.model_path):
                local_model.load(wp)
            return True
        except Exception as e:
            logger.debug("Secondary model sync failed: %s", e)
            return False

    @staticmethod
    def _use_rtsp_cuda_decode() -> bool:
        """
        CUDA FFmpeg decode for OpenCV RTSP (JPEG + detection pipeline).
        RTSP_HWACCEL: request GPU decode when CUDA is available (still false on CPU-only torch).
        If RTSP_HWACCEL is False, RTSP_HWACCEL_AUTO enables the same when torch.cuda.is_available().
        """
        try:
            import torch

            cuda_ok = bool(torch.cuda.is_available())
        except Exception:
            cuda_ok = False
        if bool(getattr(settings, "RTSP_HWACCEL", False)):
            return cuda_ok
        if not bool(getattr(settings, "RTSP_HWACCEL_AUTO", True)):
            return False
        return cuda_ok

    @classmethod
    def _prefer_ffmpeg_rtsp_decode(cls, is_rtsp: bool) -> bool:
        if not is_rtsp:
            return False
        if bool(getattr(settings, "RTSP_FFMPEG_PIPE_DECODE", False)):
            return True
        from app.services.ffmpeg_relay_service import use_h264_ffmpeg_burnin

        return use_h264_ffmpeg_burnin()

    @classmethod
    def _reopen_ffmpeg_rtsp_capture(
        cls,
        cap,
        url: str,
        *,
        capture_label: str,
    ):
        from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

        try:
            cap.release()
        except Exception:
            pass
        cap = cls._open_video_capture(
            url,
            is_rtsp=True,
            prefer_ffmpeg=True,
            capture_label=capture_label,
        )
        return cap, isinstance(cap, FFmpegRtspCapture)

    @classmethod
    def _read_capture_frame(cls, cap, *, is_rtsp: bool) -> tuple[bool, np.ndarray | None]:
        from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

        if is_rtsp and isinstance(cap, FFmpegRtspCapture):
            return cap.read_fresh(int(getattr(settings, "RTSP_FLUSH_FRAMES", 0) or 0))
        ret, fr = cap.read()
        if not ret or fr is None or not is_rtsp:
            return ret, fr
        for _ in range(max(0, int(settings.RTSP_FLUSH_FRAMES))):
            if not cap.grab():
                break
        ret2, fresh = cap.retrieve()
        if ret2 and fresh is not None:
            return True, fresh
        return ret, fr

    @classmethod
    def _rtsp_ffmpeg_options_base(cls) -> str:
        return (
            "rtsp_transport;tcp|buffer_size;4096000|max_delay;500000|stimeout;5000000"
            "|fflags;discardcorrupt|flags;low_delay|err_detect;ignore_err"
        )

    @classmethod
    def _rtsp_ffmpeg_options(cls) -> str:
        """
        Build FFmpeg capture options for OpenCV.
        When CUDA decode is enabled: hwaccel cuda + device + extra_hw_frames (more work on GPU, less CPU).
        """
        base = cls._rtsp_ffmpeg_options_base()
        if not cls._use_rtsp_cuda_decode():
            return base
        dev = max(0, int(getattr(settings, "RTSP_CUDA_DEVICE", 0) or 0))
        try:
            extra = int(getattr(settings, "RTSP_CUDA_EXTRA_FRAMES", 16) or 0)
        except (TypeError, ValueError):
            extra = 16
        extra = max(0, min(extra, 64))
        opts = base + f"|hwaccel;cuda|hwaccel_device;{dev}|hwaccel_output_format;cuda"
        if extra > 0:
            opts += f"|extra_hw_frames;{extra}"
        return opts

    @classmethod
    def _rtsp_ffmpeg_options_d3d11(cls) -> str:
        return cls._rtsp_ffmpeg_options_base() + "|hwaccel;d3d11va"

    @classmethod
    def warm_rtsp_ffmpeg_env(cls) -> None:
        """Set OPENCV_FFMPEG_CAPTURE_OPTIONS once at startup (before any capture thread)."""
        import os

        opts = cls._rtsp_ffmpeg_options()
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = opts
        if cls._use_rtsp_cuda_decode():
            logger.info(
                "RTSP: CUDA decode hints active (torch CUDA; see RTSP_CUDA_DEVICE / RTSP_CUDA_EXTRA_FRAMES). "
                "If open/read fails, set RTSP_HWACCEL=false and RTSP_HWACCEL_AUTO=false."
            )
        else:
            logger.info(
                "RTSP: software decode (no torch CUDA, or RTSP_HWACCEL/RTSP_HWACCEL_AUTO disabled)."
            )

    @staticmethod
    def _append_ffmpeg_opts(opts: str, extra: str) -> str:
        extra = (extra or "").strip()
        if not extra:
            return opts
        if extra.startswith("|"):
            return opts + extra
        return opts + "|" + extra

    def _open_video_capture(
        self,
        url: str,
        *,
        is_rtsp: bool,
        ffmpeg_extra: str = "",
        prefer_ffmpeg: bool | None = None,
        capture_label: str = "primary",
    ) -> cv2.VideoCapture:
        """
        Open RTSP with HW decode: FFmpeg hwaccel (CUDA, then D3D11 on Windows), then CPU.
        Avoids OpenCV VIDEO_ACCELERATION_ANY + CAP_PROP_HW_DEVICE (invalid combo on Windows builds).
        """
        if not is_rtsp:
            return cv2.VideoCapture(url)

        import os
        import sys

        use_ffmpeg = (
            self._prefer_ffmpeg_rtsp_decode(is_rtsp)
            if prefer_ffmpeg is None
            else bool(prefer_ffmpeg)
        )
        if use_ffmpeg and is_rtsp:
            from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

            cap_ff: FFmpegRtspCapture | None = None
            try:
                cap_ff = FFmpegRtspCapture(url, label=capture_label)
                if cap_ff.isOpened():
                    logger.info("StreamService: RTSP via FFmpeg BGR pipe [%s]", capture_label)
                    return cap_ff
            except Exception as e:
                logger.warning("FFmpeg RTSP capture failed: %s", e)
            from app.services.ffmpeg_rtsp_decode import ffmpeg_cuda_required

            err = getattr(cap_ff, "_last_error", None) if cap_ff is not None else str(
                "FFmpegRtspCapture init failed"
            )
            if cap_ff is not None and cap_ff.isOpened():
                pass
            elif ffmpeg_cuda_required() and prefer_ffmpeg is not False:
                logger.error(
                    "StreamService: [CUDA FAILED] [%s] %s — no OpenCV/CPU fallback",
                    capture_label,
                    err,
                )
                return cap_ff if cap_ff is not None else cv2.VideoCapture()
            elif cap_ff is not None and not cap_ff.isOpened():
                logger.warning(
                    "FFmpeg RTSP not opened [%s]: %s — trying OpenCV",
                    capture_label,
                    err,
                )

        from app.services.ffmpeg_rtsp_decode import ffmpeg_cuda_required

        # Thumbnails / preview (prefer_ffmpeg=False): OpenCV OK — not live CUDA pipe.
        if is_rtsp and ffmpeg_cuda_required() and prefer_ffmpeg is not False:
            logger.error(
                "StreamService: CUDA RTSP required — skip OpenCV/D3D11/CPU capture [%s]",
                capture_label,
            )
            return cv2.VideoCapture()

        base = self._append_ffmpeg_opts(self._rtsp_ffmpeg_options_base(), ffmpeg_extra)

        def _try_ffmpeg_capture(ffmpeg_opts: str) -> cv2.VideoCapture | None:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = ffmpeg_opts
            try:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if cap.isOpened():
                    return cap
                cap.release()
            except Exception as e:
                logger.debug("FFmpeg VideoCapture failed (%s): %s", ffmpeg_opts[:48], e)
            return None

        cap_prop_hw_accel = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
        cap_prop_hw_device = getattr(cv2, "CAP_PROP_HW_DEVICE", None)
        video_accel_d3d11 = getattr(cv2, "VIDEO_ACCELERATION_D3D11", None)

        def _try_opencv_hw_capture(hw_flag: int, label: str) -> cv2.VideoCapture | None:
            """OpenCV HW API: D3D11 only — do not pass HW_DEVICE with ANY."""
            if cap_prop_hw_accel is None or hw_flag is None:
                return None
            params: list[int] = [int(cap_prop_hw_accel), int(hw_flag)]
            if cap_prop_hw_device is not None:
                dev = max(0, int(getattr(settings, "RTSP_CUDA_DEVICE", 0) or 0))
                params.extend([int(cap_prop_hw_device), dev])
            try:
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if cap.isOpened():
                    logger.debug("VideoCapture opened with OpenCV %s hw acceleration", label)
                    return cap
                cap.release()
            except Exception as e:
                logger.debug("OpenCV %s hw capture failed: %s", label, e)
            return None

        if self._use_rtsp_cuda_decode():
            cap = _try_ffmpeg_capture(
                self._append_ffmpeg_opts(self._rtsp_ffmpeg_options(), ffmpeg_extra)
            )
            if cap is not None:
                logger.debug("RTSP: FFmpeg CUDA hwaccel decode")
                return cap

        if sys.platform == "win32" and bool(getattr(settings, "RTSP_D3D11_FALLBACK", True)):
            cap = _try_ffmpeg_capture(
                self._append_ffmpeg_opts(self._rtsp_ffmpeg_options_d3d11(), ffmpeg_extra)
            )
            if cap is not None:
                logger.debug(
                    "RTSP: D3D11VA decode via FFmpeg (CUDA hwaccel open failed or unavailable)."
                )
                return cap
            if video_accel_d3d11 is not None:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = self._append_ffmpeg_opts(
                    self._rtsp_ffmpeg_options_d3d11(), ffmpeg_extra
                )
                cap = _try_opencv_hw_capture(int(video_accel_d3d11), "D3D11")
                if cap is not None:
                    logger.debug("RTSP: D3D11VA decode via OpenCV capture API.")
                    return cap

        cap = _try_ffmpeg_capture(base)
        if cap is not None:
            return cap
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = base
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    # ── Properties ────────────────────────────────────────────────────────────

    def _merged_total(self) -> int:
        n = int(self._counter.total) + int(self._companion_counter.total)
        for c in self._extra_counters.values():
            n += int(c.total)
        return n

    @staticmethod
    def _merge_class_maps(*maps: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in maps:
            if not m:
                continue
            for k, v in m.items():
                key = str(k)
                out[key] = out.get(key, 0) + int(v)
        return out

    def merged_vehicle_stats(self) -> VehicleStats:
        """primary + companion + extra 2/3 — dùng cho API /stats."""
        with self._merge_lock:
            base = self._stats.model_copy(deep=True)
            base.total = self._merged_total()
            base.count_in = int(self._counter.count_in) + int(self._companion_counter.count_in)
            base.count_out = int(self._counter.count_out) + int(self._companion_counter.count_out)
            for c in self._extra_counters.values():
                base.count_in += int(c.count_in)
                base.count_out += int(c.count_out)
            extra_maps = [c.by_class for c in self._extra_counters.values()]
            base.classes = self._merge_class_maps(self._counter.by_class, self._companion_counter.by_class, *extra_maps)
            extra_in = [c.by_class_in for c in self._extra_counters.values()]
            base.classes_in = self._merge_class_maps(
                self._counter.by_class_in, self._companion_counter.by_class_in, *extra_in
            )
            extra_out = [c.by_class_out for c in self._extra_counters.values()]
            base.classes_out = self._merge_class_maps(
                self._counter.by_class_out, self._companion_counter.by_class_out, *extra_out
            )
            base.counting_mode = self._counter.mode
            try:
                base.roi_active = bool(
                    roi_service.active_for("primary")
                    or roi_service.active_for("companion")
                    or any(roi_service.active_for(str(k)) for k in self._extra_counters.keys())
                )
            except Exception:
                base.roi_active = bool(roi_service.active_for("primary"))
            return base

    @property
    def stats(self) -> VehicleStats:
        return self.merged_vehicle_stats()

    @property
    def timeline(self) -> list[dict]:
        return list(self._timeline[-60:])

    @property
    def congestion_state(self):
        return self._congestion.state

    # ── Worker Thread ─────────────────────────────────────────────────────────

    def _worker(self, url: str) -> None:
        is_rtsp = str(url).lower().startswith("rtsp://")

        # Hoist imports out of the per-frame loop — importing inside a tight loop
        # causes repeated sys.modules lookups which add measurable overhead at 30fps.
        from app.services.traffic_light_service import traffic_light_service as tls
        from app.services.model_service import model_service as _model_service

        # Capture thread pushes freshest frames into a size-1 queue (drop-frame behavior).
        frame_q: Queue[np.ndarray] = Queue(maxsize=1)
        stop_evt = threading.Event()

        def _capture_loop() -> None:
            rtsp_url = url
            # ── RTSP tuning: reduce buffering + proper HEVC handling ──────────────
            cap = self._open_video_capture(rtsp_url, is_rtsp=is_rtsp, capture_label="primary")
            from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

            using_ffmpeg_cap = isinstance(cap, FFmpegRtspCapture)

            if not cap.isOpened():
                logger.error("StreamService: cannot open %s", rtsp_url)
                self._last_error = f"Không mở được stream: {rtsp_url}"
                self._stats.stream_error = "Không mở được stream."
                stop_evt.set()
                return

            consecutive_failures = 0
            MAX_RTSP_RETRIES = 30
            try:
                while self._running and not stop_evt.is_set():
                    ret, fr = self._read_capture_frame(cap, is_rtsp=is_rtsp)
                    if not ret or fr is None:
                        if is_rtsp:
                            consecutive_failures += 1
                            if using_ffmpeg_cap and consecutive_failures == 3:
                                logger.warning(
                                    "StreamService: FFmpeg RTSP read failing, reopening FFmpeg (no OpenCV fallback)"
                                )
                                cap, using_ffmpeg_cap = self._reopen_ffmpeg_rtsp_capture(
                                    cap, rtsp_url, capture_label="primary"
                                )
                                consecutive_failures = 0
                                if not cap.isOpened():
                                    self._last_error = "Không mở được stream (FFmpeg)."
                                    self._stats.stream_error = self._last_error
                                    break
                                continue
                            if consecutive_failures <= MAX_RTSP_RETRIES:
                                time.sleep(0.1)
                                continue
                            cap.release()
                            cap = self._open_video_capture(
                                rtsp_url,
                                is_rtsp=True,
                                prefer_ffmpeg=using_ffmpeg_cap,
                                capture_label="primary",
                            )
                            using_ffmpeg_cap = isinstance(cap, FFmpegRtspCapture)
                            if not cap.isOpened():
                                self._last_error = "RTSP stream đã kết thúc."
                                self._stats.stream_error = self._last_error
                                break
                            consecutive_failures = 0
                            continue
                        # local file
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok2, fr2 = cap.read()
                        if not ok2 or fr2 is None:
                            self._last_error = "Stream đã kết thúc."
                            self._stats.stream_error = self._last_error
                            break
                        fr = fr2

                    consecutive_failures = 0

                    # Drop-frame: keep only latest in queue
                    try:
                        frame_q.put(fr, block=False)
                    except Full:
                        try:
                            _ = frame_q.get(block=False)
                        except Empty:
                            pass
                        try:
                            frame_q.put(fr, block=False)
                        except Full:
                            pass
            finally:
                cap.release()
                stop_evt.set()

        cap_thread = threading.Thread(target=_capture_loop, daemon=True)
        cap_thread.start()

        # Wait for capture thread: abort only if it exited without any frame.
        open_deadline = time.time() + 15.0
        while time.time() < open_deadline and self._running and not stop_evt.is_set():
            if not cap_thread.is_alive():
                break
            if not frame_q.empty():
                break
            time.sleep(0.15)
        if stop_evt.is_set() or not self._running:
            return
        if frame_q.empty() and not cap_thread.is_alive():
            err = self._stats.stream_error or self._last_error or "Không mở được stream."
            self._stats.stream_error = str(err)
            logger.error("StreamService: capture failed — %s", err)
            return

        self._stats.stream_active = True
        try:
            tls.set_stream_live(True)
        except Exception as e:
            logger.debug("TrafficLight attach: %s", e)
        fps_cnt = 0
        cap_fps_cnt = 0
        sent_fps_cnt = 0
        infer_fps_cnt = 0
        t0 = time.time()
        t0_cap = time.time()
        t0_sent = time.time()
        t0_infer = time.time()
        infer_ms_hist: deque[float] = deque(maxlen=10)
        frame_interval = 1.0 / max(self.max_fps, 1)
        last_dets: list = []
        # Deadline-based frame pacing: advances by exactly frame_interval each iteration
        # so timing errors don't accumulate. Replaces the old sleep(frame_interval - elapsed).
        _next_deadline = time.perf_counter()
        _last_good_frame: np.ndarray | None = None
        _holdover_count = 0
        # Congestion: track last known vehicle count from inference frames only.
        # On skipped/holdover/backlog frames tracks=[] which would falsely reset the timer.
        _last_active_count: int = 0
        try:
            while self._running:
                frame, backlog = self._drain_latest_frame(
                    frame_q, timeout=min(frame_interval * 2, 0.15)
                )
                _is_holdover = False
                if frame is None:
                    if stop_evt.is_set():
                        break
                    if _last_good_frame is not None:
                        # Keep stream alive while RTSP reconnects; stop only if
                        # capture thread has fully given up.
                        if not cap_thread.is_alive():
                            break
                        frame = _last_good_frame
                        _holdover_count += 1
                        _is_holdover = True
                    else:
                        # No frame received yet at all — just wait
                        continue
                else:
                    if _holdover_count > 0:
                        # Stream just recovered — reset deadline to avoid burst catch-up
                        _next_deadline = time.perf_counter()
                    _last_good_frame = frame
                    _holdover_count = 0
                t_start = time.time()
                if not _is_holdover:
                    cap_fps_cnt += 1

                self._stats.frame_count += 1
                fps_cnt += 1
                elapsed = time.time() - t0
                if elapsed >= 1.0:
                    self._stats.fps = round(fps_cnt / elapsed, 1)
                    fps_cnt = 0
                    t0 = time.time()
                    self._push_timeline()

                # FPS capture (frames read from RTSP)
                elapsed_cap = time.time() - t0_cap
                if elapsed_cap >= 1.0:
                    self._stats.fps_capture = round(cap_fps_cnt / elapsed_cap, 1)
                    cap_fps_cnt = 0
                    t0_cap = time.time()

                # ── Optional resize before encode (reduce payload / CPU) ─────
                try:
                    max_w = int(self.max_width or 0)
                    if max_w > 0 and frame is not None and frame.shape[1] > max_w:
                        frame = resize_bgr_max_width(
                            frame,
                            max_w,
                            use_cuda=bool(getattr(settings, "STREAM_CUDA_RESIZE", False)),
                        )
                except Exception:
                    pass

                # ── Pipeline ─────────────────────────────────────────────────
                try:
                    skip_n = max(0, int(self.skip_frames or 0))
                    do_infer = not _is_holdover and not (backlog > 0)
                    if skip_n > 0 and do_infer:
                        # infer on 1 frame, then reuse last detections for next N frames
                        if self._skip_counter > 0:
                            do_infer = False
                            self._skip_counter -= 1
                        else:
                            self._skip_counter = skip_n

                    if do_infer:
                        t_infer0 = time.time()
                        # 1. Detection (+ built-in tracking when using ByteTrack/BoT-SORT)
                        if getattr(self._tracker, 'uses_builtin', True):
                            raw_dets = yolo_model.track(
                                frame,
                                conf=self.conf_threshold,
                                tracker=self._tracker.tracker_yaml,
                                persist=True,
                            )
                        else:
                            # SORT / DeepSORT: detection only; tracker assigns IDs below
                            raw_dets = yolo_model.predict(frame, conf=self.conf_threshold)

                        # 2. Convert to Track objects (with prev_cy for line-crossing)
                        tracks = self._tracker.update(raw_dets, frame)
                        infer_fps_cnt += 1
                        infer_ms = (time.time() - t_infer0) * 1000.0
                        infer_ms_hist.append(infer_ms)
                        if infer_ms_hist:
                            self._stats.avg_inference_ms = round(
                                sum(infer_ms_hist) / float(len(infer_ms_hist)), 1
                            )
                        last_dets = raw_dets
                    else:
                        raw_dets = list(last_dets)
                        tracks = []
                except Exception as pipe_err:
                    if is_tracker_state_error(pipe_err):
                        yolo_model.reset_tracker()
                        self._tracker.reset()
                    now = time.time()
                    if now - self._pipe_err_log_ts >= 5.0:
                        logger.warning("Pipeline error (skipping frame): %s", pipe_err)
                        self._pipe_err_log_ts = now
                    raw_dets = []
                    tracks = []

                # 3. ROI + Counting + Congestion
                #
                # Không ROI: đếm tất cả xe, line theo setting
                # Có ROI:    chỉ đếm xe trong ROI, line = giữa ROI
                frame_h = frame.shape[0]
                frame_w = frame.shape[1]

                if roi_service.active and roi_service.points:
                    active_tracks = [
                        t for t in tracks if roi_service.is_inside(t.cx, t.cy, slot="primary")
                    ]
                    # Counting line = giữa vùng ROI (pixel Y)
                    roi_mid = roi_service.mid_y_for("primary")
                    line_y = roi_mid if roi_mid is not None else int(frame_h * self.line_position)
                    # Cập nhật line_position để frontend vẽ đúng vị trí
                    self._stats.line_position = line_y / frame_h
                else:
                    active_tracks = tracks
                    if self._counting_line_y is None:
                        self._counting_line_y = int(frame_h * self.line_position)
                    line_y = self._counting_line_y
                    self._stats.line_position = self.line_position

                self._counter.update(active_tracks, line_y)
                self._stats.total = self._counter.total
                self._stats.count_in = self._counter.count_in
                self._stats.count_out = self._counter.count_out
                self._stats.classes = dict(self._counter.by_class)
                self._stats.classes_in = dict(self._counter.by_class_in)
                self._stats.classes_out = dict(self._counter.by_class_out)
                self._stats.counting_mode = self._counter.mode

                # TLC fixed-cycle is display-only; we intentionally do not compute
                # any per-frame TLC queue/approach inference here.

                # 4. Congestion — cùng tập xe đã lọc
                # Only update the known count on real inference frames; on skipped/holdover/backlog
                # frames tracks=[] which would falsely reset the congestion timer to zero.
                if do_infer:
                    _last_active_count = len(active_tracks)
                # Live count of vehicles currently INSIDE the ROI (0 when ROI inactive).
                # Note: self._stats.roi_active is otherwise only set on the REST /stats path,
                # so we must set it here too or the WS payload always reports roi_active=false.
                _roi_on = bool(roi_service.active and roi_service.points)
                self._stats.roi_active = _roi_on
                self._stats.roi_count = int(_last_active_count) if _roi_on else 0
                cong_state = self._congestion.update(_last_active_count)
                self._stats.congestion = CongestionInfo(
                    is_congested=cong_state.is_congested,
                    vehicle_count=cong_state.vehicle_count,
                    threshold=cong_state.threshold,
                    duration_seconds=cong_state.duration_seconds,
                    stable_duration=cong_state.stable_duration,
                    message=cong_state.message,
                    level=cong_state.level,
                )

                # 4.1 Model info for UI (avoid "No Model" flicker)
                try:
                    self._stats.model_loaded = bool(_model_service.is_loaded)
                    self._stats.model_name = str(_model_service.name or "")
                except Exception:
                    pass

                # 5. Build payload — build dict directly, skip Pydantic object creation
                # (avoids 2 loops: one to create Detection objects, one to model_dump() them)
                api_dets_dict = [
                    {
                        "bbox": {"x1": d.x1, "y1": d.y1, "x2": d.x2, "y2": d.y2},
                        "class_name": d.class_name,
                        "confidence": d.confidence,
                        "track_id": d.track_id,
                    }
                    for d in raw_dets
                ]

                # 5.1 Feed stopped-count-in-ROI to traffic-light service (phase mapping happens there)
                # Only compute when ROI is active — skip the per-frame loop otherwise
                try:
                    if roi_service.active_for("primary"):
                        stopped_cnt, total_cnt = self._compute_stopped_in_roi("primary", raw_dets)
                        tls.update_lane_observation("primary", stopped_cnt, total_cnt)
                except Exception:
                    pass

                # 5.2 H264 bgr_burnin — reuse already-serialized dicts (no second model_dump)
                if should_h264_burnin():
                    try:
                        vis_h264 = overlay_traffic_ui(
                            frame,
                            api_dets_dict,
                            line_y_px=int(line_y),
                            show_line=not roi_service.active_for("primary"),
                            in_place=True,
                        )
                        h264_bgr_primary.write_frame(vis_h264, fps=max(1, int(self.max_fps)))
                    except Exception as he:
                        logger.warning("H264 burn-in primary skipped: %s", he)

                # Build stats snapshot without deep copy — read fields directly from _stats
                # merged_vehicle_stats() does model_copy(deep=True) which is expensive at 30fps
                _s = self._stats
                stats_dict = {
                    "total": self._merged_total(),
                    "count_in": int(self._counter.count_in) + int(self._companion_counter.count_in),
                    "count_out": int(self._counter.count_out) + int(self._companion_counter.count_out),
                    "classes": dict(_s.classes),
                    "classes_in": dict(_s.classes_in),
                    "classes_out": dict(_s.classes_out),
                    "counting_mode": _s.counting_mode,
                    "fps": _s.fps,
                    "fps_capture": _s.fps_capture,
                    "fps_inference": _s.fps_inference,
                    "fps_sent": _s.fps_sent,
                    "avg_inference_ms": _s.avg_inference_ms,
                    "frame_count": _s.frame_count,
                    "stream_active": _s.stream_active,
                    "model_loaded": _s.model_loaded,
                    "model_name": _s.model_name,
                    "roi_active": _s.roi_active,
                    "roi_count": int(_s.roi_count),
                    "conf_threshold": _s.conf_threshold,
                    "line_position": _s.line_position,
                    "congestion": {
                        "is_congested": _s.congestion.is_congested,
                        "vehicle_count": _s.congestion.vehicle_count,
                        "threshold": _s.congestion.threshold,
                        "duration_seconds": _s.congestion.duration_seconds,
                        "stable_duration": _s.congestion.stable_duration,
                        "message": _s.congestion.message,
                        "level": _s.congestion.level,
                    },
                }

                header = {
                    "detections": api_dets_dict,
                    "stats": stats_dict,
                }

                with self._lock:
                    self._latest = {
                        "frame": None,
                        "detections": api_dets_dict,
                        "stats": stats_dict,
                    }

                try:
                    if ws_manager.has_clients:
                        ws_manager.broadcast_text_threadsafe(stats_message_json(header))
                        sent_fps_cnt += 1
                except Exception:
                    pass

                # FPS sent (broadcast attempts per second)
                elapsed_sent = time.time() - t0_sent
                if elapsed_sent >= 1.0:
                    self._stats.fps_sent = round(sent_fps_cnt / elapsed_sent, 1)
                    sent_fps_cnt = 0
                    t0_sent = time.time()

                # FPS inference (YOLO calls per second)
                elapsed_infer = time.time() - t0_infer
                if elapsed_infer >= 1.0:
                    self._stats.fps_inference = round(infer_fps_cnt / elapsed_infer, 1)
                    infer_fps_cnt = 0
                    t0_infer = time.time()

                # Deadline-based throttle — avoids error accumulation and works around
                # Windows time.sleep ~15ms granularity via a short busy-wait at the end.
                _next_deadline += frame_interval
                _wait = _next_deadline - time.perf_counter()
                if _wait > 0.002:
                    time.sleep(_wait - 0.001)
                while time.perf_counter() < _next_deadline:
                    pass
                # If we fell behind (e.g. slow inference or long stall), reset
                # to avoid a burst of back-to-back frames trying to catch up.
                if time.perf_counter() - _next_deadline > frame_interval:
                    _next_deadline = time.perf_counter()
        except Exception as e:
            logger.exception("StreamService: worker crashed: %s", e)
            self._last_error = f"Worker error: {e}"
            self._stats.stream_error = str(e)
        finally:
            stop_evt.set()
            try:
                cap_thread.join(timeout=1.5)
            except Exception:
                pass
            with self._lock:
                self._latest = None          # clear stale frame
            self._stats.stream_active = False
            self._stats.fps = 0.0
            self._running = False
            try:
                tls.set_stream_live(False)
            except Exception as e:
                logger.debug("TrafficLight detach: %s", e)
            logger.info("StreamService: worker exited")

    def _companion_worker(self, url: str) -> None:
        """Second RTSP: same worker architecture as primary (capture queue + drop-frame)."""
        logger.info("StreamService: companion lane worker → %s", url[:80])
        self._sync_secondary_model(self._companion_yolo)
        is_rtsp = str(url).lower().startswith("rtsp://")
        frame_q: Queue[np.ndarray] = Queue(maxsize=1)
        stop_evt = threading.Event()

        def _capture_loop() -> None:
            rtsp_url = url
            cap = self._open_video_capture(rtsp_url, is_rtsp=is_rtsp, capture_label="companion")
            from app.services.ffmpeg_rtsp_capture import FFmpegRtspCapture

            using_ffmpeg_cap = isinstance(cap, FFmpegRtspCapture)

            if not cap.isOpened():
                logger.warning("StreamService: companion cannot open stream")
                stop_evt.set()
                return

            consecutive_failures = 0
            max_rtsp_retries = 30
            try:
                while self._companion_running and not stop_evt.is_set():
                    ret, fr = self._read_capture_frame(cap, is_rtsp=is_rtsp)
                    if not ret or fr is None:
                        if is_rtsp:
                            consecutive_failures += 1
                            if using_ffmpeg_cap and consecutive_failures == 3:
                                logger.warning(
                                    "StreamService: companion FFmpeg read failing, reopening FFmpeg"
                                )
                                cap, using_ffmpeg_cap = self._reopen_ffmpeg_rtsp_capture(
                                    cap, rtsp_url, capture_label="companion"
                                )
                                consecutive_failures = 0
                                if not cap.isOpened():
                                    break
                                continue
                            if consecutive_failures <= max_rtsp_retries:
                                time.sleep(0.1)
                                continue
                            cap.release()
                            cap = self._open_video_capture(
                                rtsp_url,
                                is_rtsp=True,
                                capture_label="companion",
                            )
                            using_ffmpeg_cap = isinstance(cap, FFmpegRtspCapture)
                            if not cap.isOpened():
                                break
                            consecutive_failures = 0
                            continue
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok2, fr2 = cap.read()
                        if not ok2 or fr2 is None:
                            break
                        fr = fr2

                    consecutive_failures = 0

                    try:
                        frame_q.put(fr, block=False)
                    except Full:
                        try:
                            _ = frame_q.get(block=False)
                        except Empty:
                            pass
                        try:
                            frame_q.put(fr, block=False)
                        except Full:
                            pass
            finally:
                cap.release()
                stop_evt.set()

        cap_thread = threading.Thread(target=_capture_loop, daemon=True)
        cap_thread.start()

        fps_cnt = 0
        t0 = time.time()
        frame_interval = 1.0 / max(int(getattr(settings, "COMPANION_MAX_FPS", 12)), 1)
        skip_counter = 0
        last_dets: list = []
        _next_deadline = time.perf_counter()
        _last_companion_active_count: int = 0  # hold count across skipped/backlog frames

        try:
            while self._companion_running:
                t_start = time.time()
                fr, backlog = self._drain_latest_frame(frame_q, timeout=1.0)
                if fr is None:
                    if stop_evt.is_set():
                        break
                    continue

                self._companion_frame_count += 1
                fps_cnt += 1
                elapsed = time.time() - t0
                if elapsed >= 1.0:
                    self._companion_fps = round(fps_cnt / elapsed, 1)
                    fps_cnt = 0
                    t0 = time.time()

                try:
                    max_w = int(self.max_width or 0)
                    if max_w > 0 and fr is not None and fr.shape[1] > max_w:
                        fr = resize_bgr_max_width(
                            fr,
                            max_w,
                            use_cuda=bool(getattr(settings, "STREAM_CUDA_RESIZE", False)),
                        )
                except Exception:
                    pass

                dets = []
                tracks = []
                if bool(getattr(settings, "COMPANION_DETECT_ENABLED", True)):
                    do_infer = not (backlog > 0)
                    comp_skip = getattr(settings, "COMPANION_SKIP_FRAMES", None)
                    skip_n = max(
                        0,
                        int(
                            comp_skip
                            if comp_skip is not None
                            else (self.skip_frames or 0)
                        ),
                    )
                    if skip_n > 0 and do_infer:
                        if skip_counter > 0:
                            do_infer = False
                            skip_counter -= 1
                        else:
                            skip_counter = skip_n

                    if do_infer:
                        try:
                            if self._sync_secondary_model(self._companion_yolo):
                                if getattr(self._companion_tracker, 'uses_builtin', True):
                                    dets = self._companion_yolo.track(
                                        fr,
                                        conf=self.conf_threshold,
                                        tracker=self._companion_tracker.tracker_yaml,
                                        persist=True,
                                    )
                                else:
                                    dets = self._companion_yolo.predict(fr, conf=self.conf_threshold)
                                tracks = self._companion_tracker.update(dets, fr)
                            else:
                                dets = []
                                tracks = []
                        except Exception as ce:
                            if is_tracker_state_error(ce):
                                self._companion_yolo.reset_tracker()
                                self._companion_tracker.reset()
                            logger.debug("Companion lane track skipped: %s", ce)
                            dets = []
                            tracks = []
                        last_dets = dets
                    else:
                        dets = list(last_dets)
                        tracks = []

                # Optional: publish companion frame + detections for UI second panel
                try:
                    # Build dict directly — skip Pydantic object creation (same as primary)
                    api_dets_dict = [
                        {
                            "bbox": {"x1": d.x1, "y1": d.y1, "x2": d.x2, "y2": d.y2},
                            "class_name": d.class_name,
                            "confidence": d.confidence,
                            "track_id": d.track_id,
                        }
                        for d in dets
                    ]
                    # Keep companion pipeline parity with primary:
                    # counting line + class counters + congestion + model info.
                    frame_h = fr.shape[0]
                    if roi_service.active_for("companion") and roi_service.points_for("companion"):
                        active_tracks = [
                            t for t in tracks if roi_service.is_inside(t.cx, t.cy, slot="companion")
                        ]
                        roi_mid = roi_service.mid_y_for("companion")
                        companion_line_y = roi_mid if roi_mid is not None else int(frame_h * self.line_position)
                    else:
                        active_tracks = tracks
                        if self._companion_counting_line_y is None:
                            self._companion_counting_line_y = int(frame_h * self.line_position)
                        companion_line_y = self._companion_counting_line_y
                    self._companion_counter.update(active_tracks, companion_line_y)
                    # Only update count on real inference; skipped frames have tracks=[] which
                    # would falsely reset the congestion timer.
                    if do_infer:
                        _last_companion_active_count = len(active_tracks)
                    companion_cong = self._companion_congestion.update(_last_companion_active_count)
                    companion_congestion_payload = {
                        "is_congested": companion_cong.is_congested,
                        "vehicle_count": companion_cong.vehicle_count,
                        "threshold": companion_cong.threshold,
                        "duration_seconds": companion_cong.duration_seconds,
                        "stable_duration": companion_cong.stable_duration,
                        "message": companion_cong.message,
                        "level": companion_cong.level,
                    }
                    companion_model_loaded = False
                    companion_model_name = ""
                    try:
                        from app.services.model_service import model_service

                        companion_model_loaded = bool(model_service.is_loaded)
                        companion_model_name = str(model_service.name or "")
                    except Exception:
                        pass
                    try:
                        if roi_service.active_for("companion"):
                            from app.services.traffic_light_service import traffic_light_service as tls
                            stopped_cnt, total_cnt = self._compute_stopped_in_roi("companion", dets)
                            tls.update_lane_observation("companion", stopped_cnt, total_cnt)
                    except Exception:
                        pass
                    if should_h264_burnin():
                        try:
                            vis_h264 = overlay_traffic_ui(
                                fr,
                                api_dets_dict,
                                line_y_px=int(companion_line_y),
                                show_line=not roi_service.active_for("companion"),
                                in_place=True,
                            )
                            c_fps = max(1, int(getattr(settings, "COMPANION_MAX_FPS", 12)))
                            h264_bgr_companion.write_frame(vis_h264, fps=c_fps)
                        except Exception as he:
                            logger.debug("H264 burn-in companion skipped: %s", he)
                    header = {
                        "detections": api_dets_dict,
                        "fps": float(self._companion_fps),
                        "frame_count": int(self._companion_frame_count),
                        "stream_active": True,
                        "lane_stats": {
                            "total": int(self._companion_counter.total),
                            "count_in": int(self._companion_counter.count_in),
                            "count_out": int(self._companion_counter.count_out),
                            "classes": dict(self._companion_counter.by_class),
                            "classes_in": dict(self._companion_counter.by_class_in),
                            "classes_out": dict(self._companion_counter.by_class_out),
                            "counting_mode": self._companion_counter.mode,
                            "line_position": (float(companion_line_y) / float(max(frame_h, 1))),
                            "congestion": companion_congestion_payload,
                            "model_loaded": companion_model_loaded,
                            "model_name": companion_model_name,
                        },
                    }
                    with self._companion_lock:
                        self._companion_latest = {
                            "frame": None,
                            "detections": header["detections"],
                            "fps": header["fps"],
                            "frame_count": header["frame_count"],
                            "stream_active": True,
                        }
                    try:
                        if ws_companion_manager.has_clients:
                            ws_companion_manager.broadcast_text_threadsafe(stats_message_json(header))
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug("Companion publish skipped: %s", e)

                sleep_t = frame_interval - (time.time() - t_start)
                if sleep_t > 0:
                    time.sleep(sleep_t)
                # Deadline-based pacing — same as primary worker to avoid drift
                _next_deadline += frame_interval
                _wait = _next_deadline - time.perf_counter()
                if _wait > 0.002:
                    time.sleep(_wait - 0.001)
                while time.perf_counter() < _next_deadline:
                    pass
                if time.perf_counter() - _next_deadline > frame_interval:
                    _next_deadline = time.perf_counter()
        finally:
            stop_evt.set()
            try:
                cap_thread.join(timeout=1.5)
            except Exception:
                pass
            with self._companion_lock:
                self._companion_latest = None
            self._companion_running = False
            logger.info("StreamService: companion lane worker exited")

    def _extra_worker(self, slot: int, url: str) -> None:
        """Extra LIVE stream for additional screens (tracking + frame)."""
        logger.info("StreamService: extra slot %d worker → %s", int(slot), str(url)[:80])
        s = int(slot)
        if s not in self._extra_yolo:
            from app.ml.yolo_model import YOLOModel
            self._extra_yolo[s] = YOLOModel()
        self._sync_secondary_model(self._extra_yolo[s])
        is_rtsp = str(url).lower().startswith("rtsp://")
        cap = self._open_video_capture(
            url, is_rtsp=is_rtsp, capture_label=f"extra{int(slot)}"
        )
        if not cap.isOpened():
            logger.warning("StreamService: extra slot %d cannot open stream", int(slot))
            with self._extra_lock:
                self._extra_running[int(slot)] = False
            return

        fps_cnt = 0
        t0 = time.time()
        frame_interval = 1.0 / max(int(getattr(settings, "EXTRA_MAX_FPS", 12)), 1)
        extra_skip_counter = 0
        extra_last_dets: list = []

        while True:
            with self._extra_lock:
                if not self._extra_running.get(int(slot), False):
                    break
            t_start = time.time()
            ok, fr = self._read_capture_frame(cap, is_rtsp=is_rtsp)
            if not ok or fr is None:
                continue

            try:
                max_w = int(self.max_width or 0)
                if max_w > 0 and fr is not None and fr.shape[1] > max_w:
                    fr = resize_bgr_max_width(
                        fr,
                        max_w,
                        use_cuda=bool(getattr(settings, "STREAM_CUDA_RESIZE", False)),
                    )
            except Exception:
                pass

            fps_cnt += 1
            elapsed = time.time() - t0
            fps_val = 0.0
            if elapsed >= 1.0:
                fps_val = round(fps_cnt / elapsed, 1)
                fps_cnt = 0
                t0 = time.time()

            do_infer = True
            skip_n = max(0, int(self.skip_frames or 0))
            if skip_n > 0:
                if extra_skip_counter > 0:
                    do_infer = False
                    extra_skip_counter -= 1
                else:
                    extra_skip_counter = skip_n

            # Lazy-init tracker before detect so we can check uses_builtin
            if s not in self._extra_trackers:
                self._extra_trackers[s] = get_tracker(self._tracker_type)

            dets = []
            try:
                if do_infer and self._sync_secondary_model(self._extra_yolo[s]):
                    if getattr(self._extra_trackers[s], 'uses_builtin', True):
                        dets = self._extra_yolo[s].track(
                            fr,
                            conf=self.conf_threshold,
                            tracker=self._extra_trackers[s].tracker_yaml,
                            persist=True,
                        )
                    else:
                        dets = self._extra_yolo[s].predict(fr, conf=self.conf_threshold)
                    extra_last_dets = dets
                elif not do_infer:
                    dets = extra_last_dets
                else:
                    dets = []
                    extra_last_dets = []
            except Exception:
                dets = []
                extra_last_dets = []

            try:
                if s not in self._extra_counters:
                    ec = VehicleCounter()
                    ec.set_mode(self._counter.mode)
                    self._extra_counters[s] = ec
                tracks = self._extra_trackers[s].update(dets, fr)
                frame_h = int(fr.shape[0])
                slot_key = str(int(slot))
                if roi_service.active_for(slot_key) and len(roi_service.points_for(slot_key)) >= 3:
                    active_tracks = [
                        t for t in tracks if roi_service.is_inside(t.cx, t.cy, slot=slot_key)
                    ]
                    roi_mid = roi_service.mid_y_for(slot_key)
                    line_y = float(roi_mid if roi_mid is not None else frame_h * self.line_position)
                else:
                    active_tracks = tracks
                    if self._extra_counting_line_y.get(s) is None:
                        self._extra_counting_line_y[s] = int(frame_h * self.line_position)
                    line_y = float(self._extra_counting_line_y[s])
                self._extra_counters[s].update(active_tracks, line_y)
            except Exception as xec:
                logger.debug("extra slot %d VehicleCounter: %s", s, xec)

            try:
                from app.models.detection_model import Detection, BoundingBox
                api_dets = [
                    Detection(
                        bbox=BoundingBox(x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2),
                        class_name=d.class_name,
                        confidence=d.confidence,
                        track_id=d.track_id,
                    )
                    for d in dets
                ]
                try:
                    from app.services.traffic_light_service import traffic_light_service as tls

                    stopped_cnt, total_cnt = self._compute_stopped_in_roi(str(int(slot)), dets)
                    tls.update_lane_observation(str(int(slot)), stopped_cnt, total_cnt)
                except Exception:
                    pass
                if should_h264_burnin():
                    try:
                        vis_h264 = overlay_traffic_ui(
                            fr,
                            [d.model_dump() for d in api_dets],
                            line_y_px=int(line_y),
                            show_line=not roi_service.active_for(slot_key),
                        )
                        xf = max(1, int(getattr(settings, "EXTRA_MAX_FPS", 12)))
                        (h264_bgr_extra2 if s == 2 else h264_bgr_extra3).write_frame(vis_h264, fps=xf)
                    except Exception as he:
                        logger.debug("H264 burn-in extra slot %d skipped: %s", s, he)
                header = {
                    "slot": int(slot),
                    "detections": [d.model_dump() for d in api_dets],
                    "fps": float(fps_val),
                    "stream_active": True,
                }
                with self._extra_lock:
                    self._extra_latest[int(slot)] = {
                        "slot": int(slot),
                        "frame": None,
                        "detections": header["detections"],
                        "fps": header["fps"],
                        "stream_active": True,
                    }
                if ws_manager.has_clients:
                    ws_manager.broadcast_text_threadsafe(stats_message_json(header))
            except Exception:
                pass

            sleep_t = frame_interval - (time.time() - t_start)
            if sleep_t > 0:
                time.sleep(sleep_t)

        cap.release()
        with self._extra_lock:
            self._extra_latest.pop(int(slot), None)
            self._extra_yolo.pop(int(slot), None)
        self._extra_trackers.pop(int(slot), None)
        self._extra_counters.pop(int(slot), None)
        self._extra_counting_line_y.pop(int(slot), None)
        logger.info("StreamService: extra slot %d worker exited", int(slot))

    def _compute_stopped_in_roi(self, slot_key: str, dets: list) -> tuple[int, int]:
        """
        Compute stopped vehicles count inside ROI for a given slot.
        Uses per-track centroid motion (px/s) over time.
        """
        key = str(slot_key)
        # If ROI is not explicitly active for this slot, we do not infer "red"/queue from motion.
        # This matches the UI expectation: must draw ROI for each camera to enable TLC advice.
        try:
            if not roi_service.active_for(key):
                return 0, 0
        except Exception:
            return 0, 0
        now = time.monotonic()
        hist = self._stop_hist.get(key)
        if hist is None:
            hist = {}
            self._stop_hist[key] = hist

        # Build active set from detections inside ROI
        active_ids: set[int] = set()
        in_roi: list = []
        for d in dets or []:
            tid = getattr(d, "track_id", None)
            if tid is None:
                continue
            if not roi_service.is_inside(getattr(d, "cx", 0), getattr(d, "cy", 0), slot=key):
                continue
            active_ids.add(int(tid))
            in_roi.append(d)

        stop_speed = float(getattr(settings, "TLC_STOP_SPEED_PX_S", 8.0) or 8.0)
        min_frames = int(getattr(settings, "TLC_MIN_STOPPED_FRAMES", 5) or 5)

        for d in in_roi:
            tid = int(d.track_id)
            cx = float(getattr(d, "cx", 0.0))
            cy = float(getattr(d, "cy", 0.0))
            prev = hist.get(tid)
            if not prev:
                hist[tid] = {"cx": cx, "cy": cy, "t": now, "streak": 0}
                continue
            dt = max(1e-3, float(now - float(prev.get("t", now))))
            dx = cx - float(prev.get("cx", cx))
            dy = cy - float(prev.get("cy", cy))
            sp = (dx * dx + dy * dy) ** 0.5 / dt
            streak = int(prev.get("streak", 0))
            if sp <= stop_speed:
                streak += 1
            else:
                streak = 0
            prev.update({"cx": cx, "cy": cy, "t": now, "streak": streak})

        # Prune tracks not present
        for tid in list(hist.keys()):
            if tid not in active_ids:
                hist.pop(tid, None)

        stopped_cnt = sum(1 for v in hist.values() if int(v.get("streak", 0)) >= min_frames)
        return int(stopped_cnt), int(len(in_roi))

    # ── Helpers ───────────────────────────────────────────────────────────────

    # ── Video file source ─────────────────────────────────────────────────────

    def start_video_file(self, path: str) -> None:
        """Upload a local video file and start detection+tracking on it."""
        self._last_error = None
        self._stats.stream_error = ""
        t = threading.Thread(target=self._do_start_video_file, args=(path,), daemon=True)
        t.start()
        logger.info("StreamService: video file start requested → %s", path)

    def _do_start_video_file(self, path: str) -> None:
        self.stop()
        self._reset_all()
        self._running = True
        self._source_mode = "file"
        self._thread = threading.Thread(target=self._video_file_worker, args=(path,), daemon=True)
        self._thread.start()
        logger.info("StreamService: video file worker started")

    def _video_file_worker(self, path: str) -> None:
        """Read frames from a video file, run detection/tracking, write to H264 pipe + WS."""
        import os
        from app.services.model_service import model_service as _model_service

        try:
            tls = None
            try:
                from app.services.traffic_light_service import traffic_light_service as _tls
                tls = _tls
            except Exception:
                pass

            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                self._last_error = f"Không thể mở file video: {path}"
                self._stats.stream_error = self._last_error
                logger.warning("StreamService: %s", self._last_error)
                self._running = False
                return

            video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            frame_interval = 1.0 / max(1.0, min(float(self.max_fps), video_fps))
            self._stats.stream_active = True
            fps_cnt = 0
            t0 = time.time()
            _next_deadline = time.perf_counter()

            while self._running:
                ret, frame = cap.read()
                if not ret:
                    break

                frame = np.array(frame)  # ensure writable

                # Detection / tracking
                try:
                    if getattr(self._tracker, 'uses_builtin', True):
                        raw_dets = yolo_model.track(
                            frame,
                            conf=self.conf_threshold,
                            tracker=self._tracker.tracker_yaml,
                            persist=True,
                        )
                    else:
                        raw_dets = yolo_model.predict(frame, conf=self.conf_threshold)
                    tracks = self._tracker.update(raw_dets, frame)
                except Exception as pipe_err:
                    if is_tracker_state_error(pipe_err):
                        yolo_model.reset_tracker()
                        self._tracker.reset()
                    raw_dets = []
                    tracks = []

                # Counting line + ROI filter (mirrors primary _worker logic)
                frame_h = frame.shape[0]
                if roi_service.active and roi_service.points:
                    active_tracks = [
                        t for t in tracks if roi_service.is_inside(t.cx, t.cy, slot="primary")
                    ]
                    roi_mid = roi_service.mid_y_for("primary")
                    line_y = roi_mid if roi_mid is not None else int(frame_h * self.line_position)
                    self._stats.line_position = line_y / frame_h
                else:
                    active_tracks = tracks
                    if self._counting_line_y is None:
                        self._counting_line_y = int(frame_h * self.line_position)
                    line_y = self._counting_line_y
                    self._stats.line_position = self.line_position

                self._counter.update(active_tracks, line_y)
                self._stats.total = self._counter.total
                self._stats.count_in = self._counter.count_in
                self._stats.count_out = self._counter.count_out
                self._stats.classes = dict(self._counter.by_class)
                self._stats.classes_in = dict(self._counter.by_class_in)
                self._stats.classes_out = dict(self._counter.by_class_out)

                # Congestion — video file mode (tracks always fresh per frame)
                _vid_cong = self._congestion.update(len(active_tracks))
                self._stats.congestion = CongestionInfo(
                    is_congested=_vid_cong.is_congested,
                    vehicle_count=_vid_cong.vehicle_count,
                    threshold=_vid_cong.threshold,
                    duration_seconds=_vid_cong.duration_seconds,
                    stable_duration=_vid_cong.stable_duration,
                    message=_vid_cong.message,
                    level=_vid_cong.level,
                )

                try:
                    self._stats.model_loaded = bool(_model_service.is_loaded)
                    self._stats.model_name = str(_model_service.name or "")
                except Exception:
                    pass

                fps_cnt += 1
                elapsed = time.time() - t0
                if elapsed >= 1.0:
                    self._stats.fps = round(fps_cnt / elapsed, 1)
                    fps_cnt = 0
                    t0 = time.time()

                self._stats.frame_count = getattr(self._stats, 'frame_count', 0) + 1

                api_dets_dict = [
                    {
                        "bbox": {"x1": d.x1, "y1": d.y1, "x2": d.x2, "y2": d.y2},
                        "class_name": d.class_name,
                        "confidence": d.confidence,
                        "track_id": d.track_id,
                    }
                    for d in raw_dets
                ]

                # H264 burn-in
                if should_h264_burnin():
                    try:
                        vis = overlay_traffic_ui(
                            frame,
                            api_dets_dict,
                            line_y_px=int(line_y),
                            show_line=not roi_service.active_for("primary"),
                            in_place=True,
                        )
                        h264_bgr_primary.write_frame(vis, fps=max(1, int(video_fps)))
                    except Exception as he:
                        logger.warning("H264 burn-in video file skipped: %s", he)

                _s = self._stats
                _vc = _s.congestion
                stats_dict = {
                    "total": int(_s.total),
                    "count_in": int(_s.count_in),
                    "count_out": int(_s.count_out),
                    "classes": dict(_s.classes),
                    "classes_in": dict(_s.classes_in),
                    "classes_out": dict(_s.classes_out),
                    "counting_mode": _s.counting_mode,
                    "fps": _s.fps,
                    "fps_capture": 0.0,
                    "fps_inference": 0.0,
                    "fps_sent": 0.0,
                    "avg_inference_ms": _s.avg_inference_ms,
                    "frame_count": _s.frame_count,
                    "stream_active": _s.stream_active,
                    "model_loaded": _s.model_loaded,
                    "model_name": _s.model_name,
                    "roi_active": roi_service.active_for("primary"),
                    "conf_threshold": _s.conf_threshold,
                    "line_position": _s.line_position,
                    "congestion": {
                        "is_congested": _vc.is_congested,
                        "vehicle_count": _vc.vehicle_count,
                        "threshold": _vc.threshold,
                        "duration_seconds": _vc.duration_seconds,
                        "stable_duration": _vc.stable_duration,
                        "message": _vc.message,
                        "level": _vc.level,
                    },
                }
                header = {"detections": api_dets_dict, "stats": stats_dict}
                with self._lock:
                    self._latest = {"frame": None, "detections": api_dets_dict, "stats": stats_dict}
                try:
                    if ws_manager.has_clients:
                        ws_manager.broadcast_text_threadsafe(stats_message_json(header))
                except Exception:
                    pass

                # Deadline-based frame pacing
                _next_deadline += frame_interval
                _wait = _next_deadline - time.perf_counter()
                if _wait > 0.002:
                    time.sleep(_wait - 0.001)
                while time.perf_counter() < _next_deadline:
                    pass
                if time.perf_counter() - _next_deadline > frame_interval:
                    _next_deadline = time.perf_counter()

        except Exception as e:
            logger.exception("StreamService: video file worker crashed: %s", e)
            self._last_error = f"Video worker error: {e}"
            self._stats.stream_error = str(e)
        finally:
            try:
                cap.release()
            except Exception:
                pass
            # Delete temp upload file
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.info("StreamService: deleted temp video %s", path)
            except Exception:
                pass
            with self._lock:
                self._latest = None
            # Notify frontend that video playback has ended
            try:
                if ws_manager.has_clients:
                    _s = self._stats
                    _vc = _s.congestion
                    end_stats = {
                        "total": int(_s.total),
                        "count_in": int(_s.count_in),
                        "count_out": int(_s.count_out),
                        "classes": dict(_s.classes),
                        "classes_in": dict(_s.classes_in),
                        "classes_out": dict(_s.classes_out),
                        "counting_mode": _s.counting_mode,
                        "fps": 0.0,
                        "fps_capture": 0.0,
                        "fps_inference": 0.0,
                        "fps_sent": 0.0,
                        "avg_inference_ms": 0.0,
                        "frame_count": int(getattr(_s, "frame_count", 0)),
                        "stream_active": False,
                        "model_loaded": bool(_s.model_loaded),
                        "model_name": str(_s.model_name or ""),
                        "roi_active": roi_service.active_for("primary"),
                        "conf_threshold": float(_s.conf_threshold),
                        "line_position": float(_s.line_position),
                        "congestion": {
                            "is_congested": _vc.is_congested,
                            "vehicle_count": _vc.vehicle_count,
                            "threshold": _vc.threshold,
                            "duration_seconds": _vc.duration_seconds,
                            "stable_duration": _vc.stable_duration,
                            "message": _vc.message,
                            "level": _vc.level,
                        },
                    }
                    ws_manager.broadcast_text_threadsafe(
                        stats_message_json({"video_ended": True, "detections": [], "stats": end_stats})
                    )
                    time.sleep(0.1)
            except Exception:
                pass
            self._stats.stream_active = False
            self._stats.fps = 0.0
            self._running = False
            self._source_mode = "rtsp"
            logger.info("StreamService: video file worker exited")

    def _reset_all(self) -> None:
        self._tracker = get_tracker(self._tracker_type)
        self._tracker.reset()
        self._companion_tracker = get_tracker(self._tracker_type)
        self._companion_tracker.reset()
        yolo_model.reset_tracker()
        self._counter.reset()
        self._companion_counter.reset()
        self._congestion.reset()
        self._companion_congestion.reset()
        self._stats = VehicleStats(
            conf_threshold=self.conf_threshold,
            line_position=self.line_position,
        )
        self._counting_line_y = None
        self._companion_counting_line_y = None
        self._timeline.clear()
        self._timeline_last = 0
        self._companion_frame_count = 0
        self._companion_fps = 0.0
        for tr in list(self._extra_trackers.values()):
            try:
                tr.reset()
            except Exception:
                pass
        self._extra_trackers.clear()
        for c in list(self._extra_counters.values()):
            c.reset()
        self._extra_counters.clear()
        self._extra_counting_line_y.clear()
        with self._lock:
            self._latest = None
        with self._companion_lock:
            self._companion_latest = None

    def _push_timeline(self) -> None:
        mt = self._merged_total()
        delta = mt - self._timeline_last
        self._timeline.append({"t": int(time.time()), "v": max(0, delta)})
        self._timeline_last = mt


# Singleton
stream_service = StreamService()
