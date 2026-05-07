"""
Stream Service – video capture thread + full detection pipeline.

Pipeline per frame:
  OpenCV → YOLOModel.track() (built-in ByteTrack) → VehicleCounter → CongestionMonitor
"""

from __future__ import annotations
import base64
import asyncio
import json
import threading
import time

import cv2
import numpy as np

from app.core.config import settings
from app.core.logger import logger
from app.models.detection_model import VehicleStats, CongestionInfo

# ── ML Layer ──────────────────────────────────────────────────────────────────
from app.ml.yolo_model import yolo_model
from app.ml.tracker import get_tracker
from app.ml.vehicle_counter import VehicleCounter
from app.ml.congestion_monitor import CongestionMonitor

# ── Service Layer ─────────────────────────────────────────────────────────────
from app.services.roi_service import roi_service
from app.utils.video_utils import is_youtube_url, resolve_youtube_url, validate_youtube_url
from app.api.ws_routes import ws_manager, ws_companion_manager


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

        # ML sub-components
        self._tracker = get_tracker(self._tracker_type)
        self._counter = VehicleCounter()
        self._congestion = CongestionMonitor(
            vehicle_threshold=settings.CONGESTION_VEHICLE_THRESHOLD,
            stable_duration=settings.CONGESTION_STABLE_DURATION,
        )

        # Runtime state
        self._stats = VehicleStats()
        self._counting_line_y: int | None = None
        self._timeline: list[dict] = []
        self._timeline_last: int = 0
        self._last_error: str | None = None
        # Legacy: stored event loop for optional WS broadcasting (may be unused)
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # For inference skipping (keep last detections to reuse on skipped frames)
        self._skip_counter: int = 0
        self._last_api_dets: list[dict] = []
        # Optional second RTSP — same intersection, other approach (video only)
        self._companion_thread: threading.Thread | None = None
        self._companion_running: bool = False
        self._companion_url: str = ""
        self._companion_latest: dict | None = None
        self._companion_lock = threading.Lock()
        self._companion_frame_count: int = 0
        self._companion_fps: float = 0.0

        # Extra live streams (screens 3/4): each has its own worker + latest payload
        self._extra_threads: dict[int, threading.Thread] = {}
        self._extra_running: dict[int, bool] = {}
        self._extra_urls: dict[int, str] = {}
        self._extra_latest: dict[int, dict] = {}
        self._extra_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

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

    def stop_extra(self, slot: int) -> None:
        s = int(slot)
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
                logger.info("StreamService: companion TLC lane → %s", self._companion_url[:80])
            self._thread = threading.Thread(target=self._worker, args=(effective_url,), daemon=True)
            self._thread.start()
            logger.info("StreamService: worker started")
        except Exception as e:
            self._last_error = str(e)
            self._stats.stream_error = str(e)
            self._running = False
            logger.exception("StreamService: start failed: %s", e)

    def stop(self) -> None:
        self._running = False
        self._companion_running = False
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
        yolo_model.reset_tracker()
        self._congestion.reset()
        self._stats.total = 0
        self._stats.count_in = 0
        self._stats.count_out = 0
        self._stats.classes = {}
        self._stats.classes_in = {}
        self._stats.classes_out = {}
        self._stats.congestion = CongestionInfo()
        self._timeline.clear()
        self._timeline_last = 0
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
    ) -> None:
        if conf is not None:
            self.conf_threshold = max(0.1, min(0.95, conf))
        if line is not None:
            self.line_position = max(0.05, min(0.95, line))
            self._counting_line_y = None
        if fps is not None:
            self.max_fps = max(1, min(60, fps))
        if tracker_type is not None:
            t = str(tracker_type).strip().lower()
            if t in ("bytetrack", "botsort"):
                self._tracker_type = t
                self._tracker = get_tracker(t)
                yolo_model.reset_tracker()
        if counting_mode is not None:
            self._counter.set_mode(counting_mode)
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

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> VehicleStats:
        return self._stats

    @property
    def timeline(self) -> list[dict]:
        return list(self._timeline[-60:])

    @property
    def congestion_state(self):
        return self._congestion.state

    # ── Worker Thread ─────────────────────────────────────────────────────────

    def _worker(self, url: str) -> None:
        is_rtsp = str(url).lower().startswith("rtsp://")

        # ── RTSP tuning: reduce buffering + proper HEVC handling ──────────────
        rtsp_url = url
        if is_rtsp:
            # Use TCP transport (more reliable than UDP for HEVC)
            # max_delay: reduce jitter buffer for live stream
            # stimeout: socket timeout 5s to detect dead streams fast
            if "?" not in url and "rtsp_transport" not in url:
                # Inject FFmpeg options via OpenCV environment
                import os
                os.environ.setdefault(
                    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                    "rtsp_transport;tcp|buffer_size;4096000|max_delay;500000|stimeout;5000000|fflags;nobuffer|flags;low_delay"
                )
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            # Minimize buffer to keep frames fresh
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Do NOT force H264 fourcc — let FFmpeg auto-detect HEVC/H264
        else:
            cap = cv2.VideoCapture(url)

        if not cap.isOpened():
            logger.error("StreamService: cannot open %s", url)
            self._last_error = f"Không mở được stream: {url}"
            self._stats.stream_error = "Không mở được stream."
            self._running = False
            return

        self._stats.stream_active = True
        try:
            from app.services.traffic_light_service import traffic_light_service as tls

            tls.set_stream_live(True)
        except Exception as e:
            logger.debug("TrafficLight attach: %s", e)
        fps_cnt = 0
        t0 = time.time()
        frame_interval = 1.0 / max(self.max_fps, 1)
        consecutive_failures = 0
        MAX_RTSP_RETRIES = 30      # ~3s of retries at 0.1s each

        try:
            while self._running:
                t_start = time.time()
                ret, frame = cap.read()

                if not ret:
                    if is_rtsp:
                        consecutive_failures += 1
                        if consecutive_failures <= MAX_RTSP_RETRIES:
                            logger.debug("RTSP read failed (%d/%d), retrying...",
                                         consecutive_failures, MAX_RTSP_RETRIES)
                            time.sleep(0.1)
                            continue
                        # Try reopening the capture
                        logger.warning("RTSP: %d failures, reopening capture...", consecutive_failures)
                        cap.release()
                        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        if not cap.isOpened():
                            self._last_error = "RTSP stream đã kết thúc."
                            self._stats.stream_error = self._last_error
                            break
                        consecutive_failures = 0
                        continue
                    else:
                        # Local file → loop back to start
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, frame_retry = cap.read()
                        if not ok:
                            self._last_error = "Stream đã kết thúc."
                            self._stats.stream_error = self._last_error
                            break
                        frame = frame_retry

                consecutive_failures = 0  # reset on success

                # ── RTSP: grab extra frames to flush buffer lag ────────────────
                # When processing is slower than camera FPS, old frames pile up.
                # Grab (but don't decode) until buffer is nearly empty so we
                # always process the freshest frame.
                if is_rtsp:
                    for _ in range(max(0, int(settings.RTSP_FLUSH_FRAMES))):
                        grabbed = cap.grab()
                        if not grabbed:
                            break
                    ret2, fresh = cap.retrieve()
                    if ret2 and fresh is not None:
                        frame = fresh

                self._stats.frame_count += 1
                fps_cnt += 1
                elapsed = time.time() - t0
                if elapsed >= 1.0:
                    self._stats.fps = round(fps_cnt / elapsed, 1)
                    fps_cnt = 0
                    t0 = time.time()
                    self._push_timeline()

                # ── Optional resize before encode (reduce payload / CPU) ─────
                try:
                    max_w = int(getattr(settings, "STREAM_MAX_WIDTH", 0) or 0)
                    if max_w > 0 and frame is not None and frame.shape[1] > max_w:
                        h, w = frame.shape[:2]
                        new_h = max(1, int(h * (max_w / float(w))))
                        frame = cv2.resize(frame, (max_w, new_h), interpolation=cv2.INTER_AREA)
                except Exception:
                    pass

                # ── Pipeline ─────────────────────────────────────────────────
                try:
                    skip_n = max(0, int(getattr(settings, "INFERENCE_SKIP_FRAMES", 0) or 0))
                    do_infer = True
                    if skip_n > 0:
                        # infer on 1 frame, then reuse last detections for next N frames
                        if self._skip_counter > 0:
                            do_infer = False
                            self._skip_counter -= 1
                        else:
                            self._skip_counter = skip_n

                    if do_infer:
                        # 1. Detection + Tracking (built-in ByteTrack/BoT-SORT)
                        raw_dets = yolo_model.track(
                            frame,
                            conf=self.conf_threshold,
                            tracker=self._tracker.tracker_yaml,
                            persist=True,
                        )

                        # 2. Convert to Track objects (with prev_cy for line-crossing)
                        tracks = self._tracker.update(raw_dets)
                    else:
                        raw_dets = []
                        tracks = []
                except Exception as pipe_err:
                    logger.warning("Pipeline error (skipping frame): %s", pipe_err)
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
                        t for t in tracks if roi_service.is_inside(t.cx, t.cy)
                    ]
                    # Counting line = giữa vùng ROI (pixel Y)
                    roi_mid = roi_service.mid_y
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
                cong_state = self._congestion.update(len(active_tracks))
                self._stats.congestion = CongestionInfo(
                    is_congested=cong_state.is_congested,
                    vehicle_count=cong_state.vehicle_count,
                    threshold=cong_state.threshold,
                    duration_seconds=cong_state.duration_seconds,
                    stable_duration=cong_state.stable_duration,
                    message=cong_state.message,
                    level=cong_state.level,
                )

                # 5. Build payload
                from app.models.detection_model import Detection, BoundingBox

                api_dets = [
                    Detection(
                        bbox=BoundingBox(x1=d.x1, y1=d.y1, x2=d.x2, y2=d.y2),
                        class_name=d.class_name,
                        confidence=d.confidence,
                        track_id=d.track_id,
                    )
                    for d in raw_dets
                ]

                # 6. Encode frame (lower quality = smaller payload = higher FPS)
                jpeg_q = int(getattr(settings, "STREAM_JPEG_QUALITY", 75) or 75)
                jpeg_q = max(30, min(95, jpeg_q))
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])
                frame_b64 = base64.b64encode(buf.tobytes()).decode()

                payload = {
                    "frame": frame_b64,
                    "detections": [d.model_dump() for d in api_dets],
                    "stats": self._stats.model_dump(),
                }

                with self._lock:
                    self._latest = payload

                # Broadcast via WebSocket when clients are connected
                try:
                    if ws_manager.has_clients and self._event_loop is not None:
                        msg = json.dumps(payload, ensure_ascii=False)
                        self._event_loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(ws_manager.broadcast(msg))
                        )
                except Exception:
                    pass

                # Throttle
                sleep_t = frame_interval - (time.time() - t_start)
                if sleep_t > 0:
                    time.sleep(sleep_t)
        except Exception as e:
            logger.exception("StreamService: worker crashed: %s", e)
            self._last_error = f"Worker error: {e}"
            self._stats.stream_error = str(e)
        finally:
            cap.release()
            with self._lock:
                self._latest = None          # clear stale frame
            self._stats.stream_active = False
            self._stats.fps = 0.0
            self._running = False
            try:
                from app.services.traffic_light_service import traffic_light_service as tls

                tls.set_stream_live(False)
            except Exception as e:
                logger.debug("TrafficLight detach: %s", e)
            logger.info("StreamService: worker exited")

    def _companion_worker(self, url: str) -> None:
        """Second RTSP: YOLO predict-only + optional live frame payload for UI."""
        logger.info("StreamService: companion lane worker → %s", url[:80])
        is_rtsp = str(url).lower().startswith("rtsp://")
        if is_rtsp:
            import os

            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                "rtsp_transport;tcp|buffer_size;4096000|max_delay;500000|stimeout;5000000|fflags;nobuffer|flags;low_delay",
            )
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            logger.warning("StreamService: companion cannot open stream")
            self._companion_running = False
            return

        fps_cnt = 0
        t0 = time.time()
        frame_interval = 1.0 / max(int(getattr(settings, "COMPANION_MAX_FPS", 12)), 1)
        while self._companion_running:
            t_start = time.time()
            ok, fr = cap.read()
            if not ok or fr is None:
                continue

            # RTSP: flush a bit to keep it fresh when inference is slower than camera FPS
            if is_rtsp:
                for _ in range(max(0, int(settings.RTSP_FLUSH_FRAMES))):
                    if not cap.grab():
                        break
                ret2, fresh = cap.retrieve()
                if ret2 and fresh is not None:
                    fr = fresh

            self._companion_frame_count += 1
            fps_cnt += 1
            elapsed = time.time() - t0
            if elapsed >= 1.0:
                self._companion_fps = round(fps_cnt / elapsed, 1)
                fps_cnt = 0
                t0 = time.time()
            dets = []
            if bool(getattr(settings, "COMPANION_DETECT_ENABLED", True)):
                try:
                    dets = yolo_model.predict(fr, conf=self.conf_threshold)
                except Exception as ce:
                    logger.debug("Companion lane predict skipped: %s", ce)
                    dets = []

            # Optional: publish companion frame + detections for UI second panel
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
                jpeg_q = int(getattr(settings, "STREAM_JPEG_QUALITY", 75) or 75)
                jpeg_q = max(30, min(95, jpeg_q))
                _, buf = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])
                frame_b64 = base64.b64encode(buf.tobytes()).decode()
                payload = {
                    "frame": frame_b64,
                    "detections": [d.model_dump() for d in api_dets],
                    "fps": float(self._companion_fps),
                    "frame_count": int(self._companion_frame_count),
                    "stream_active": True,
                }
                with self._companion_lock:
                    self._companion_latest = payload
            except Exception as e:
                logger.debug("Companion publish skipped: %s", e)

            # Broadcast companion via WebSocket
            try:
                if ws_companion_manager.has_clients and self._event_loop is not None:
                    msg = json.dumps(payload, ensure_ascii=False)
                    self._event_loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(ws_companion_manager.broadcast(msg))
                    )
            except Exception:
                pass

            # Throttle (independent of primary)
            sleep_t = frame_interval - (time.time() - t_start)
            if sleep_t > 0:
                time.sleep(sleep_t)

        cap.release()
        with self._companion_lock:
            self._companion_latest = None
        logger.info("StreamService: companion lane worker exited")

    def _extra_worker(self, slot: int, url: str) -> None:
        """Extra LIVE stream for additional screens (detect + frame)."""
        logger.info("StreamService: extra slot %d worker → %s", int(slot), str(url)[:80])
        is_rtsp = str(url).lower().startswith("rtsp://")
        if is_rtsp:
            import os
            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                "rtsp_transport;tcp|buffer_size;4096000|max_delay;500000|stimeout;5000000|fflags;nobuffer|flags;low_delay",
            )
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            logger.warning("StreamService: extra slot %d cannot open stream", int(slot))
            with self._extra_lock:
                self._extra_running[int(slot)] = False
            return

        fps_cnt = 0
        t0 = time.time()
        frame_interval = 1.0 / max(int(getattr(settings, "EXTRA_MAX_FPS", 12)), 1)

        while True:
            with self._extra_lock:
                if not self._extra_running.get(int(slot), False):
                    break
            t_start = time.time()
            ok, fr = cap.read()
            if not ok or fr is None:
                continue
            if is_rtsp:
                for _ in range(max(0, int(settings.RTSP_FLUSH_FRAMES))):
                    if not cap.grab():
                        break
                ret2, fresh = cap.retrieve()
                if ret2 and fresh is not None:
                    fr = fresh

            fps_cnt += 1
            elapsed = time.time() - t0
            fps_val = 0.0
            if elapsed >= 1.0:
                fps_val = round(fps_cnt / elapsed, 1)
                fps_cnt = 0
                t0 = time.time()

            dets = []
            try:
                dets = yolo_model.predict(fr, conf=self.conf_threshold)
            except Exception:
                dets = []

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
                jpeg_q = int(getattr(settings, "STREAM_JPEG_QUALITY", 75) or 75)
                jpeg_q = max(30, min(95, jpeg_q))
                _, buf = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_q])
                frame_b64 = base64.b64encode(buf.tobytes()).decode()
                payload = {
                    "slot": int(slot),
                    "frame": frame_b64,
                    "detections": [d.model_dump() for d in api_dets],
                    "fps": float(fps_val),
                    "stream_active": True,
                }
                with self._extra_lock:
                    self._extra_latest[int(slot)] = payload
                # Broadcast on main WS with slot tag (reuse ws_manager)
                if ws_manager.has_clients and self._event_loop is not None:
                    msg = json.dumps(payload, ensure_ascii=False)
                    self._event_loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(ws_manager.broadcast(msg))
                    )
            except Exception:
                pass

            sleep_t = frame_interval - (time.time() - t_start)
            if sleep_t > 0:
                time.sleep(sleep_t)

        cap.release()
        with self._extra_lock:
            self._extra_latest.pop(int(slot), None)
        logger.info("StreamService: extra slot %d worker exited", int(slot))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reset_all(self) -> None:
        self._tracker = get_tracker(self._tracker_type)
        self._tracker.reset()
        yolo_model.reset_tracker()
        self._counter.reset()
        self._congestion.reset()
        self._stats = VehicleStats(
            conf_threshold=self.conf_threshold,
            line_position=self.line_position,
        )
        self._counting_line_y = None
        self._timeline.clear()
        self._timeline_last = 0
        self._companion_frame_count = 0
        self._companion_fps = 0.0
        with self._lock:
            self._latest = None
        with self._companion_lock:
            self._companion_latest = None

    def _push_timeline(self) -> None:
        delta = self._stats.total - self._timeline_last
        self._timeline.append({"t": int(time.time()), "v": max(0, delta)})
        self._timeline_last = self._stats.total


# Singleton
stream_service = StreamService()
