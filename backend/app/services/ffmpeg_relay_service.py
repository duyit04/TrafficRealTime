from __future__ import annotations

import os
import subprocess
import threading
import time
import shutil
from collections import deque
from typing import Optional

from app.core.logger import logger
from app.api.ws_routes import get_h264_manager


class FFmpegRelayService:
    """
    Relay RTSP -> MPEG-TS(H264 NVENC) over WebSocket bytes.
    Keeps /stream/frame as fallback path.
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
        if not u:
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
        logger.info("FFmpegRelay: started for URL")

    def stop(self) -> None:
        with self._lock:
            self._running = False
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
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
            }

    def _worker(self) -> None:
        with self._lock:
            url = self._url
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
        if not ffmpeg_bin:
            with self._lock:
                self._last_error = "ffmpeg not found (set FFMPEG_BIN or add ffmpeg to PATH)"
                self._running = False
            return
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-i",
            url,
            "-an",
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-rc",
            "vbr",
            "-b:v",
            "2500k",
            "-maxrate",
            "3500k",
            "-bufsize",
            "2500k",
            "-g",
            "30",
            "-f",
            "mpegts",
            "pipe:1",
        ]
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
            logger.warning("FFmpegRelay: spawn failed: %s", e)
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
                chunk = proc.stdout.read(188 * 7)
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
            logger.debug("FFmpegRelay worker error: %s", e)
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
        """
        Drain FFmpeg stderr continuously to avoid pipe backpressure deadlock.
        Keep a short tail for diagnostics.
        """
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

