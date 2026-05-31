"""
H264 MPEG-TS over WebSocket from BGR frames (same pipeline as OpenCV/YOLO).

BGR (CPU RAM) -> FFmpeg [optional hwupload_cuda] -> h264_nvenc (GPU) -> MPEG-TS bytes.
Boxes are drawn on the frame before encode so video matches detections.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from queue import Empty, Full, Queue
from typing import Optional

import numpy as np

from app.core.config import settings
from app.core.logger import logger
from app.api.ws_routes import get_h264_manager
from app.services.ffmpeg_rtsp_decode import resolve_ffmpeg_bin


def _nvenc_output_args() -> list[str]:
    gpu = (os.environ.get("H264_FFMPEG_GPU") or "").strip() or str(max(0, int(getattr(settings, "H264_FFMPEG_GPU", 0))))
    try:
        surfaces = int(os.environ.get("H264_NVENC_SURFACES") or getattr(settings, "H264_NVENC_SURFACES", 32))
    except ValueError:
        surfaces = int(getattr(settings, "H264_NVENC_SURFACES", 32))
    surfaces = max(0, min(surfaces, 64))
    preset = (os.environ.get("H264_NVENC_PRESET") or "").strip() or str(
        getattr(settings, "H264_NVENC_PRESET", "p1") or "p1"
    )
    bitrate = (os.environ.get("H264_NVENC_BITRATE") or "").strip() or str(
        getattr(settings, "H264_NVENC_BITRATE", "2500k") or "2500k"
    )
    maxrate = (os.environ.get("H264_NVENC_MAXRATE") or "").strip() or str(
        getattr(settings, "H264_NVENC_MAXRATE", "3500k") or "3500k"
    )
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
        "ull",
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
        "-rc-lookahead",
        "0",
        "-delay",
        "0",
        "-zerolatency",
        "1",
        "-b_ref_mode",
        "0",
        "-multipass",
        "0",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def _build_cmd(ffmpeg_bin: str, w: int, h: int, fps: int) -> list[str]:
    fps = max(1, min(int(fps), 120))
    gpu_raw = (os.environ.get("H264_FFMPEG_GPU") or "").strip() or str(max(0, int(getattr(settings, "H264_FFMPEG_GPU", 0))))
    try:
        gi = int(gpu_raw)
    except ValueError:
        gi = 0
    gi = max(0, gi)

    cmd: list[str] = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-video_size",
        f"{w}x{h}",
        "-framerate",
        str(fps),
        "-thread_queue_size",
        "4",
        "-i",
        "pipe:0",
    ]
    if bool(getattr(settings, "H264_CUDA_PIPE_UPLOAD", True)):
        # bgr24 must be converted to nv12 on CPU before hwupload_cuda can accept it
        cmd += ["-vf", f"format=nv12,hwupload_cuda=device={gi}"]
    else:
        # h264_nvenc does not accept bgr24 directly; force explicit BGR24→NV12 via libswscale
        # so FFmpeg does not auto-pick a format that swaps R/B channels (e.g. 0rgb32 vs 0bgr32)
        cmd += ["-vf", "format=nv12"]
    cmd.extend(_nvenc_output_args())
    return cmd


def _build_cpu_cmd(ffmpeg_bin: str, w: int, h: int, fps: int) -> list[str]:
    """libx264 CPU fallback — used when NVENC is unavailable or crashes immediately."""
    fps = max(1, min(int(fps), 120))
    bitrate = (os.environ.get("H264_NVENC_BITRATE") or "").strip() or str(
        getattr(settings, "H264_NVENC_BITRATE", "2500k") or "2500k"
    )
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-video_size",
        f"{w}x{h}",
        "-framerate",
        str(fps),
        "-thread_queue_size",
        "4",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-b:v",
        bitrate,
        "-g",
        "30",
        "-f",
        "mpegts",
        "pipe:1",
    ]


class H264BgrMpegTsPipe:
    """
    stdin raw BGR24 frames -> h264_nvenc -> stdout MPEG-TS.
    stdin.write is outside the mutex so the stdout reader thread never deadlocks.
    """

    def __init__(self, slot: str = "primary") -> None:
        self._slot = (slot or "primary").strip().lower()
        self._proc: Optional[subprocess.Popen] = None
        self._reader_t: Optional[threading.Thread] = None
        self._stderr_t: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._dims: Optional[tuple[int, int, int]] = None
        self._bytes_sent = 0
        self._started_at = 0.0
        self._last_error: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._write_q: Queue[bytes | None] = Queue(maxsize=4)
        self._writer_t: Optional[threading.Thread] = None
        self._nvenc_failed: bool = False

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._dims = None
        if proc is not None:
            try:
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._reader_t:
            self._reader_t.join(timeout=2)
            self._reader_t = None
        if self._stderr_t:
            self._stderr_t.join(timeout=1.5)
            self._stderr_t = None
        if self._writer_t:
            while True:
                try:
                    self._write_q.get_nowait()
                except Empty:
                    break
            try:
                self._write_q.put_nowait(None)
            except Full:
                pass
            self._writer_t.join(timeout=2)
            self._writer_t = None

    def status(self) -> dict:
        with self._lock:
            running = bool(self._proc is not None and self._proc.poll() is None)
            elapsed = max(0.001, time.time() - self._started_at) if self._started_at else 0.0
            throughput_kbps = (self._bytes_sent * 8.0 / elapsed / 1000.0) if elapsed > 0 else 0.0
            return {
                "running": running,
                "url_set": bool(self._dims),
                "bytes_sent": int(self._bytes_sent),
                "throughput_kbps": round(float(throughput_kbps), 1),
                "last_error": self._last_error,
                "pipeline": "opencv_bgr_nvenc",
                "cuda_pipe_upload": bool(getattr(settings, "H264_CUDA_PIPE_UPLOAD", True)),
            }

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

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        first_chunk = True
        try:
            if proc.stdout is None:
                logger.warning("H264BgrPipe(%s): stdout is None — no output will be produced", self._slot)
                return
            while True:
                chunk = proc.stdout.read(188 * 64)
                if not chunk:
                    if proc.poll() is not None:
                        rc = proc.returncode
                        with self._lock:
                            err = self._last_error
                            tail = list(self._stderr_tail)
                            uptime = time.time() - (self._started_at or 0)
                        detail = " | ".join(tail[-3:]) if tail else (err or "")
                        logger.warning(
                            "H264BgrPipe(%s): encoder exited rc=%d%s",
                            self._slot, rc,
                            f" — {detail}" if detail else "",
                        )
                        # first_chunk=True means this process never produced MPEG-TS output.
                        # If it also exited non-zero quickly, NVENC likely failed on first frame;
                        # flag it so subsequent _spawn calls fall back to libx264.
                        if rc != 0 and first_chunk and uptime < 30.0:
                            with self._lock:
                                self._nvenc_failed = True
                            logger.warning(
                                "H264BgrPipe(%s): NVENC produced no output — future spawns will use libx264",
                                self._slot,
                            )
                        break
                    continue
                if first_chunk:
                    first_chunk = False
                    logger.info("H264BgrPipe(%s): first MPEG-TS bytes flowing (%d B)", self._slot, len(chunk))
                with self._lock:
                    if self._proc is not proc:
                        break
                    self._bytes_sent += len(chunk)
                try:
                    get_h264_manager(self._slot).broadcast_bytes_threadsafe(chunk)
                except Exception:
                    pass
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            logger.warning("H264BgrPipe(%s) reader error: %s", self._slot, e)

    def _shutdown_proc(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _spawn(self, w: int, h: int, fps: int) -> bool:
        ffmpeg_bin = resolve_ffmpeg_bin()
        if not ffmpeg_bin:
            with self._lock:
                self._last_error = "ffmpeg not found (set FFMPEG_BIN or add ffmpeg to PATH)"
            logger.warning("H264BgrPipe(%s): ffmpeg not found — video will not stream", self._slot)
            return False

        with self._lock:
            nvenc_previously_failed = self._nvenc_failed
        use_nvenc = bool(getattr(settings, "H264_CUDA_PIPE_UPLOAD", True)) and not nvenc_previously_failed
        if nvenc_previously_failed:
            logger.info("H264BgrPipe(%s): NVENC previously failed — spawning libx264 directly", self._slot)
            cmd = _build_cpu_cmd(ffmpeg_bin, w, h, fps)
        else:
            cmd = _build_cmd(ffmpeg_bin, w, h, fps)
        logger.debug("H264BgrPipe(%s): spawn cmd: %s", self._slot, " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            with self._lock:
                self._last_error = f"spawn ffmpeg failed: {e}"
            logger.warning("H264BgrPipe: spawn failed: %s", e)
            return False

        if proc.stdin is None or proc.stdout is None:
            with self._lock:
                self._last_error = "ffmpeg stdio unavailable"
            try:
                proc.kill()
            except Exception:
                pass
            return False

        # When NVENC is requested, wait briefly to catch immediate crashes (bad GPU/driver).
        if use_nvenc and not nvenc_previously_failed:
            time.sleep(0.5)
            if proc.poll() is not None:
                stderr_lines: list[str] = []
                try:
                    raw_err = proc.stderr.read() if proc.stderr else b""
                    stderr_lines = raw_err.decode("utf-8", errors="ignore").splitlines()
                except Exception:
                    pass
                err_msg = " | ".join(stderr_lines[-4:]) if stderr_lines else "NVENC crashed immediately"
                logger.warning("H264BgrPipe(%s): NVENC failed (%s), falling back to libx264", self._slot, err_msg)
                try:
                    proc.kill()
                except Exception:
                    pass

                cpu_cmd = _build_cpu_cmd(ffmpeg_bin, w, h, fps)
                try:
                    proc = subprocess.Popen(
                        cpu_cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                    )
                except Exception as e2:
                    with self._lock:
                        self._last_error = f"libx264 fallback spawn failed: {e2}"
                    logger.warning("H264BgrPipe: libx264 fallback spawn failed: %s", e2)
                    return False

                if proc.stdin is None or proc.stdout is None:
                    with self._lock:
                        self._last_error = "ffmpeg libx264 stdio unavailable"
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return False

                logger.info("H264BgrPipe(%s): libx264 CPU fallback active", self._slot)

        with self._lock:
            self._proc = proc
            self._dims = (w, h, fps)
            self._started_at = time.time()
            self._bytes_sent = 0
            self._last_error = None

        if proc.stderr is not None:
            self._stderr_t = threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)
            self._stderr_t.start()

        self._reader_t = threading.Thread(target=self._reader_loop, args=(proc,), daemon=True)
        self._reader_t.start()
        if bool(getattr(settings, "H264_PIPE_ASYNC_WRITE", True)):
            self._writer_t = threading.Thread(target=self._stdin_writer_loop, daemon=True)
            self._writer_t.start()
        logger.info("H264BgrPipe(%s): started %dx%d @ %dfps", self._slot, w, h, fps)
        return True

    def _stdin_writer_loop(self) -> None:
        while True:
            try:
                payload = self._write_q.get(timeout=0.5)
            except Empty:
                continue
            if payload is None:
                break
            with self._lock:
                proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                continue
            try:
                proc.stdin.write(payload)
            except (BrokenPipeError, OSError) as e:
                with self._lock:
                    self._last_error = str(e)
                logger.warning("H264BgrPipe(%s): stdin write error: %s", self._slot, e)
                if proc is not None:
                    self._shutdown_proc(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                        self._dims = None

    def _restart_if_needed(self, w: int, h: int, fps: int) -> subprocess.Popen | None:
        """Return live proc for (w,h,fps); restart encoder under lock when dimensions or process invalid."""
        with self._lock:
            if (
                self._proc is not None
                and self._dims == (w, h, fps)
                and self._proc.poll() is None
            ):
                return self._proc
            old = self._proc
            self._proc = None
            self._dims = None

        if old is not None:
            self._shutdown_proc(old)
            if self._writer_t:
                # Drain queue then send sentinel so writer thread exits cleanly
                while True:
                    try:
                        self._write_q.get_nowait()
                    except Empty:
                        break
                try:
                    self._write_q.put_nowait(None)
                except Full:
                    pass
                self._writer_t.join(timeout=2)
                self._writer_t = None
            if self._reader_t:
                self._reader_t.join(timeout=2)
                self._reader_t = None
            if self._stderr_t:
                self._stderr_t.join(timeout=1.5)
                self._stderr_t = None

        if not self._spawn(w, h, fps):
            return None
        with self._lock:
            return self._proc

    def write_frame(self, bgr: np.ndarray, *, fps: int) -> None:
        if bgr.ndim != 3 or bgr.shape[2] != 3:
            return
        h0, w0 = int(bgr.shape[0]), int(bgr.shape[1])
        w = w0 - (w0 % 2)
        h = h0 - (h0 % 2)
        if w < 32 or h < 32:
            return
        if w != w0 or h != h0:
            bgr = np.ascontiguousarray(bgr[:h, :w])
        else:
            bgr = np.ascontiguousarray(bgr)

        proc = self._restart_if_needed(w, h, fps)
        if proc is None or proc.stdin is None:
            return
        raw = bgr.tobytes()
        if bool(getattr(settings, "H264_PIPE_ASYNC_WRITE", True)) and self._writer_t:
            try:
                self._write_q.put_nowait(raw)
            except Full:
                try:
                    _ = self._write_q.get_nowait()
                except Empty:
                    pass
                try:
                    self._write_q.put_nowait(raw)
                except Full:
                    pass
            return
        try:
            proc.stdin.write(raw)
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._last_error = str(e)
            self._shutdown_proc(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._dims = None


h264_bgr_primary = H264BgrMpegTsPipe("primary")
h264_bgr_companion = H264BgrMpegTsPipe("companion")
h264_bgr_extra2 = H264BgrMpegTsPipe("extra2")
h264_bgr_extra3 = H264BgrMpegTsPipe("extra3")
