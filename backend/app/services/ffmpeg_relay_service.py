"""
H264 MPEG-TS over WebSocket.

- ffmpeg_burnin (default): CUDA RTSP decode (same flags as rtsp_relay) → overlay boxes → NVENC.
- rtsp_relay: FFmpeg RTSP → NVENC only (no boxes in video).
- bgr_burnin: OpenCV decode + burn-in (legacy; HEVC often corrupts).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Optional

from app.core.config import settings
from app.core.logger import logger
from app.api.ws_routes import get_h264_manager

from app.services.ffmpeg_h264_bgr_pipe_service import (
    h264_bgr_primary,
    h264_bgr_companion,
    h264_bgr_extra2,
    h264_bgr_extra3,
)
from app.services.ffmpeg_rtsp_decode import (
    cuda_decode_enabled,
    cuda_hwaccel_before_input,
    rtsp_demuxer_flags,
)


def h264_pipeline_mode() -> str:
    return (getattr(settings, "H264_PIPELINE", "ffmpeg_burnin") or "ffmpeg_burnin").strip().lower()


def use_h264_rtsp_relay() -> bool:
    return h264_pipeline_mode() in {"rtsp_relay", "relay"}


def use_h264_ffmpeg_burnin() -> bool:
    return h264_pipeline_mode() in {"ffmpeg_burnin", "burnin", "default"}


def use_h264_bgr_burnin() -> bool:
    return h264_pipeline_mode() in {"bgr_burnin", "bgr", "opencv_burnin"}


def should_h264_burnin() -> bool:
    return use_h264_ffmpeg_burnin() or use_h264_bgr_burnin()


def _resolve_ffmpeg_bin() -> str:
    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip()
    if not ffmpeg_bin:
        ffmpeg_bin = shutil.which("ffmpeg") or ""
    if not ffmpeg_bin:
        user = os.environ.get("USERNAME", "")
        candidate = os.path.join(
            "C:\\Users",
            user,
            "AppData",
            "Local",
            "Microsoft",
            "WinGet",
            "Links",
            "ffmpeg.exe",
        )
        if os.path.exists(candidate):
            ffmpeg_bin = candidate
    return ffmpeg_bin


def _nvenc_relay_args() -> list[str]:
    preset = (os.environ.get("H264_NVENC_PRESET") or "").strip() or str(
        getattr(settings, "H264_NVENC_PRESET", "p1") or "p1"
    )
    bitrate = (os.environ.get("H264_NVENC_BITRATE") or "").strip() or str(
        getattr(settings, "H264_NVENC_BITRATE", "2500k") or "2500k"
    )
    maxrate = (os.environ.get("H264_NVENC_MAXRATE") or "").strip() or str(
        getattr(settings, "H264_NVENC_MAXRATE", "3500k") or "3500k"
    )
    gpu = (os.environ.get("H264_FFMPEG_GPU") or "").strip() or str(
        max(0, int(getattr(settings, "H264_FFMPEG_GPU", 0)))
    )
    try:
        surfaces = int(os.environ.get("H264_NVENC_SURFACES") or getattr(settings, "H264_NVENC_SURFACES", 32))
    except ValueError:
        surfaces = 32
    surfaces = max(0, min(surfaces, 64))
    return [
        "-an",
        "-c:v",
        "h264_nvenc",
        "-gpu",
        gpu,
        "-surfaces",
        str(surfaces),
        "-preset",
        preset,
        "-tune",
        "ll",
        "-rc",
        "vbr",
        "-b:v",
        bitrate,
        "-maxrate",
        maxrate,
        "-bufsize",
        bitrate,
        "-g",
        "30",
        "-f",
        "mpegts",
        "pipe:1",
    ]


class FFmpegRelayService:
    """
    Relay RTSP → MPEG-TS (H264 NVENC) over WebSocket.
    Decode/encode entirely in FFmpeg — does not pass through OpenCV (avoids HEVC glitches in UI).
    """

    def __init__(self, slot: str = "primary") -> None:
        self._slot = (slot or "primary").strip().lower()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running = False
        self._url = ""
        self._lock = threading.Lock()
        self._bytes_sent = 0
        self._started_at = 0.0
        self._last_error: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=12)

    def start(self, url: str) -> None:
        u = (url or "").strip()
        if not u or not u.lower().startswith("rtsp://"):
            return
        with self._lock:
            if self._running and self._url == u:
                return
        self.stop()
        with self._lock:
            self._url = u
            self._running = True
            self._bytes_sent = 0
            self._started_at = time.time()
            self._last_error = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        logger.info("FFmpegRelay(%s): started", self._slot)

    def stop(self) -> None:
        with self._lock:
            self._running = False
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1.5)
            self._stderr_thread = None

    def status(self) -> dict:
        with self._lock:
            running = bool(self._running and self._proc is not None and self._proc.poll() is None)
            elapsed = max(0.001, time.time() - self._started_at) if self._started_at else 0.0
            throughput_kbps = (self._bytes_sent * 8.0 / elapsed / 1000.0) if elapsed > 0 else 0.0
            return {
                "running": running,
                "url_set": bool(self._url),
                "bytes_sent": int(self._bytes_sent),
                "throughput_kbps": round(float(throughput_kbps), 1),
                "last_error": self._last_error,
                "pipeline": "rtsp_relay_nvenc",
            }

    def _build_cmd(self, url: str, ffmpeg_bin: str) -> list[str]:
        cmd: list[str] = [ffmpeg_bin]
        cmd.extend(rtsp_demuxer_flags())
        if cuda_decode_enabled():
            cmd.extend(cuda_hwaccel_before_input())
        cmd += ["-i", url]
        cmd.extend(_nvenc_relay_args())
        return cmd

    def _worker(self) -> None:
        with self._lock:
            url = self._url
        ffmpeg_bin = _resolve_ffmpeg_bin()
        if not ffmpeg_bin:
            with self._lock:
                self._last_error = "ffmpeg not found (set FFMPEG_BIN or add ffmpeg to PATH)"
                self._running = False
            return

        cmd = self._build_cmd(url, ffmpeg_bin)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            with self._lock:
                self._last_error = f"spawn ffmpeg failed: {e}"
                self._running = False
            logger.warning("FFmpegRelay(%s): spawn failed: %s", self._slot, e)
            return

        proc = self._proc
        if proc.stdout is None:
            with self._lock:
                self._last_error = "ffmpeg stdout unavailable"
                self._running = False
            return
        if proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(proc.stderr,),
                daemon=True,
            )
            self._stderr_thread.start()

        try:
            while True:
                with self._lock:
                    if not self._running:
                        break
                chunk = proc.stdout.read(188 * 64)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    get_h264_manager(self._slot).broadcast_bytes_threadsafe(chunk)
                except Exception:
                    pass
                with self._lock:
                    self._bytes_sent += len(chunk)
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            logger.debug("FFmpegRelay(%s) worker: %s", self._slot, e)
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            with self._lock:
                self._running = False
            self._proc = None
            self._stderr_thread = None

    def _drain_stderr(self, stream) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="ignore").strip()
                if not msg:
                    continue
                with self._lock:
                    self._stderr_tail.append(msg)
                    self._last_error = msg
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass


ffmpeg_relay_service = FFmpegRelayService("primary")
ffmpeg_relay_companion_service = FFmpegRelayService("companion")
ffmpeg_relay_extra2_service = FFmpegRelayService("extra2")
ffmpeg_relay_extra3_service = FFmpegRelayService("extra3")

__all__ = [
    "FFmpegRelayService",
    "ffmpeg_relay_service",
    "ffmpeg_relay_companion_service",
    "ffmpeg_relay_extra2_service",
    "ffmpeg_relay_extra3_service",
    "h264_bgr_primary",
    "h264_bgr_companion",
    "h264_bgr_extra2",
    "h264_bgr_extra3",
    "h264_pipeline_mode",
    "use_h264_rtsp_relay",
    "use_h264_ffmpeg_burnin",
    "use_h264_bgr_burnin",
    "should_h264_burnin",
]
