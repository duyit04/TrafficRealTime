"""
RTSP capture via FFmpeg raw BGR pipe (same decode path as H264 relay).

OpenCV often corrupts HEVC RTSP; FFmpeg decode is stable. YOLO + H264 burn-in
use the same pixels — boxes cannot drift from the video.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

_READ_TIMEOUT_SEC = 6.0

from app.core.config import settings
from app.core.logger import logger
from app.services.ffmpeg_rtsp_decode import (
    bgr_pipe_vf,
    cuda_bgr_vf_variants,
    cuda_decode_enabled,
    cuda_decoder_before_input,
    cuda_hwaccel_before_input,
    ffmpeg_cuda_required,
    rtsp_capture_timeout_flags,
    rtsp_demuxer_flags,
)

_PROBE_CACHE: dict[str, tuple[tuple[int, int], float]] = {}
_CODEC_CACHE: dict[str, tuple[str | None, float]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_TTL_SEC = 120.0

_live_slots_in_use = 0
_live_slots_lock = threading.Lock()
_thumb_slots_in_use = 0
_thumb_slots_lock = threading.Lock()


def _max_concurrent_ffmpeg() -> int:
    try:
        return max(1, min(int(getattr(settings, "FFMPEG_CAPTURE_MAX_CONCURRENT", 2) or 2), 4))
    except (TypeError, ValueError):
        return 2


def _max_concurrent_thumb_ffmpeg() -> int:
    try:
        return max(1, min(int(getattr(settings, "THUMB_FFMPEG_MAX_CONCURRENT", 1) or 1), 2))
    except (TypeError, ValueError):
        return 1


def _probe_size_cached(url: str, ffprobe_bin: str) -> tuple[int, int] | None:
    now = time.time()
    with _PROBE_CACHE_LOCK:
        hit = _PROBE_CACHE.get(url)
        if hit and (now - hit[1]) < _PROBE_TTL_SEC:
            return hit[0]
    size = _probe_size(url, ffprobe_bin)
    if size is not None:
        with _PROBE_CACHE_LOCK:
            _PROBE_CACHE[url] = (size, now)
    return size


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


def _resolve_ffprobe_bin(ffmpeg_bin: str) -> str:
    ffprobe = os.environ.get("FFPROBE_BIN", "").strip()
    if ffprobe:
        return ffprobe
    ffprobe = shutil.which("ffprobe") or ""
    if ffprobe:
        return ffprobe
    if ffmpeg_bin.lower().endswith("ffmpeg.exe"):
        candidate = ffmpeg_bin[:-10] + "ffprobe.exe"
        if os.path.exists(candidate):
            return candidate
    return ""


def _probe_codec_cached(url: str, ffprobe_bin: str) -> str | None:
    now = time.time()
    with _PROBE_CACHE_LOCK:
        hit = _CODEC_CACHE.get(url)
        if hit and (now - hit[1]) < _PROBE_TTL_SEC:
            return hit[0]
    codec = _probe_codec(url, ffprobe_bin)
    with _PROBE_CACHE_LOCK:
        _CODEC_CACHE[url] = (codec, now)
    return codec


def _probe_codec(url: str, ffprobe_bin: str) -> str | None:
    if not ffprobe_bin:
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "csv=p=0",
        url,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=12)
        name = out.decode("utf-8", errors="ignore").strip().splitlines()[0].strip().lower()
        return name or None
    except Exception as e:
        logger.debug("ffprobe codec failed: %s", e)
    return None


def _probe_size(url: str, ffprobe_bin: str) -> tuple[int, int] | None:
    if not ffprobe_bin:
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        url,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=12)
        text = out.decode("utf-8", errors="ignore").strip().splitlines()[0]
        w_s, h_s = text.split("x", 1)
        w, h = int(w_s), int(h_s)
        if w >= 32 and h >= 32:
            return w, h
    except Exception as e:
        logger.debug("ffprobe size failed: %s", e)
    return None


def _pipe_output_size(
    src_w: int, src_h: int, *, pipe_max_width: int | None = None
) -> tuple[int, int, bool]:
    """Downscale in FFmpeg so we never read multi‑MB BGR frames (e.g. 3200×1800) on the pipe."""
    if pipe_max_width is not None and int(pipe_max_width) > 0:
        max_w = int(pipe_max_width)
    else:
        try:
            max_w = int(getattr(settings, "STREAM_MAX_WIDTH", 0) or 0)
        except (TypeError, ValueError):
            max_w = 0
        if max_w <= 0:
            max_w = 1280
    src_w = max(32, int(src_w))
    src_h = max(32, int(src_h))
    if src_w <= max_w:
        w, h = src_w - (src_w % 2), src_h - (src_h % 2)
        return w, h, False
    w = max_w - (max_w % 2)
    h = int(round(src_h * (w / float(src_w))))
    h = max(32, h - (h % 2))
    return w, h, True


class FFmpegRtspCapture:
    """Drop-in replacement for cv2.VideoCapture on RTSP URLs (read/release/isOpened)."""

    def __init__(
        self,
        url: str,
        *,
        label: str = "live",
        use_live_slot: bool = True,
        pipe_max_width: int | None = None,
    ) -> None:
        self._url = (url or "").strip()
        self._label = (label or "live").strip()
        self._use_live_slot = bool(use_live_slot)
        self._pipe_max_width = int(pipe_max_width) if pipe_max_width else None
        self._slot_acquired = False
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_t: Optional[threading.Thread] = None
        self._w = 0
        self._h = 0
        self._frame_bytes = 0
        self._opened = False
        self._last_error: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=8)
        self._lock = threading.Lock()
        self._retrieve_buf: np.ndarray | None = None
        self._initial_buf: bytes = b""
        self._src_w = 0
        self._src_h = 0
        self.decode_backend: str = ""
        self._open()

    def _build_cmd(
        self,
        url: str,
        ffmpeg_bin: str,
        out_w: int,
        out_h: int,
        *,
        scaled: bool,
        use_cuda: bool,
        codec_name: str | None = None,
        explicit_cuvid: bool = False,
        vf_variant: str = "scale_cuda",
    ) -> list[str]:
        cmd: list[str] = [ffmpeg_bin]
        cmd.extend(rtsp_demuxer_flags())
        cmd.extend(rtsp_capture_timeout_flags())
        if use_cuda:
            device_frames = vf_variant not in {"sysmem"}
            cmd.extend(cuda_hwaccel_before_input(device_frames=device_frames))
            if explicit_cuvid:
                cmd.extend(cuda_decoder_before_input(codec_name))
        vf = bgr_pipe_vf(
            out_w, out_h, scaled=scaled, cuda=use_cuda, variant=vf_variant
        )
        cmd += ["-i", url, "-an", "-vf", vf]
        cmd += ["-f", "rawvideo", "pipe:1"]
        return cmd

    def _kill_proc(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _spawn_decode(
        self,
        ffmpeg_bin: str,
        out_w: int,
        out_h: int,
        *,
        scaled: bool,
        use_cuda: bool,
        codec_name: str | None = None,
        explicit_cuvid: bool = False,
        vf_variant: str = "scale_cuda",
    ) -> bool:
        cmd = self._build_cmd(
            self._url,
            ffmpeg_bin,
            out_w,
            out_h,
            scaled=scaled,
            use_cuda=use_cuda,
            codec_name=codec_name,
            explicit_cuvid=explicit_cuvid,
            vf_variant=vf_variant,
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            self._last_error = str(e)
            return False
        if proc.stdout is None:
            self._kill_proc(proc)
            self._last_error = "ffmpeg stdout unavailable"
            return False
        if proc.stderr is not None:
            self._stderr_t = threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)
            self._stderr_t.start()

        # Wait for first bytes from stdout instead of a blind 10s timer.
        # Success = FFmpeg produces data; failure = process exits (fast, usually <1s).
        frame_bytes = out_w * out_h * 3
        chunk_size = min(65536, max(4096, frame_bytes))
        _first_chunk: list[bytes] = []
        _data_event = threading.Event()

        def _read_first_chunk() -> None:
            try:
                data = proc.stdout.read(chunk_size)  # type: ignore[union-attr]
                if data:
                    _first_chunk.append(data)
            except Exception:
                pass
            _data_event.set()

        first_t = threading.Thread(target=_read_first_chunk, daemon=True)
        first_t.start()

        deadline = time.time() + 9.0
        while time.time() < deadline:
            code = proc.poll()
            if code is not None:
                _data_event.wait(timeout=0.3)
                with self._lock:
                    err = "; ".join(self._stderr_tail) if self._stderr_tail else f"ffmpeg exited {code}"
                self._last_error = err
                self._kill_proc(proc)
                self._stderr_t = None
                return False
            if _data_event.is_set():
                break
            time.sleep(0.05)

        if not _data_event.is_set():
            self._last_error = "FFmpeg no data within 9s — RTSP timeout or bad URL"
            self._kill_proc(proc)
            self._stderr_t = None
            return False

        if not _first_chunk:
            code = proc.poll()
            with self._lock:
                err = "; ".join(self._stderr_tail) if self._stderr_tail else f"ffmpeg read failed (exit={code})"
            self._last_error = err
            self._kill_proc(proc)
            self._stderr_t = None
            return False

        self._initial_buf = _first_chunk[0]
        self._proc = proc
        self._w, self._h = out_w, out_h
        self._frame_bytes = frame_bytes
        self._opened = True
        return True

    def _drain_stderr(self, stream) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="ignore").strip()
                if msg:
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

    def _open(self) -> None:
        global _live_slots_in_use, _thumb_slots_in_use
        ffmpeg_bin = _resolve_ffmpeg_bin()
        if not ffmpeg_bin:
            self._last_error = "ffmpeg not found"
            return
        if self._use_live_slot:
            max_slots = _max_concurrent_ffmpeg()
            slots_lock = _live_slots_lock
            slot_kind = "live"
        else:
            max_slots = _max_concurrent_thumb_ffmpeg()
            slots_lock = _thumb_slots_lock
            slot_kind = "thumb"
        deadline = time.time() + 25.0
        while time.time() < deadline:
            with slots_lock:
                in_use = _live_slots_in_use if self._use_live_slot else _thumb_slots_in_use
                if in_use < max_slots:
                    if self._use_live_slot:
                        _live_slots_in_use += 1
                    else:
                        _thumb_slots_in_use += 1
                    self._slot_acquired = True
                    break
            time.sleep(0.25)
        if not self._slot_acquired:
            self._last_error = f"FFmpeg {slot_kind} slots full (max {max_slots})"
            logger.warning("FFmpegRtspCapture(%s): %s", self._label, self._last_error)
            return
        # Run ffprobe size + codec in parallel to halve probe time on first connect.
        import concurrent.futures as _cf
        ffprobe_bin = _resolve_ffprobe_bin(ffmpeg_bin)
        with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
            _f_size = _ex.submit(_probe_size_cached, self._url, ffprobe_bin)
            _f_codec = _ex.submit(_probe_codec_cached, self._url, ffprobe_bin)
            size = _f_size.result()
            codec_name = _f_codec.result()

        if size is None:
            try:
                max_w = int(getattr(settings, "STREAM_MAX_WIDTH", 1280) or 1280)
            except (TypeError, ValueError):
                max_w = 1280
            src_w, src_h = max_w, int(max_w * 9 / 16)
            logger.warning(
                "FFmpegRtspCapture[%s]: ffprobe failed, assume %dx%d",
                self._label,
                src_w,
                src_h,
            )
        else:
            src_w, src_h = size
        out_w, out_h, scaled = _pipe_output_size(src_w, src_h, pipe_max_width=self._pipe_max_width)
        self._src_w, self._src_h = src_w, src_h
        want_cuda = cuda_decode_enabled()
        opened = False
        decode = ""

        if want_cuda:
            for vf_var in cuda_bgr_vf_variants(out_w, out_h, scaled=scaled):
                if opened:
                    break
                opened = self._spawn_decode(
                    ffmpeg_bin,
                    out_w,
                    out_h,
                    scaled=scaled,
                    use_cuda=True,
                    codec_name=codec_name,
                    vf_variant=vf_var,
                )
                if opened:
                    decode = f"cuda_{vf_var}:{codec_name or 'auto'}"
                    break
                logger.warning(
                    "FFmpegRtspCapture[%s]: [CUDA] vf=%s failed: %s",
                    self._label,
                    vf_var,
                    self._last_error,
                )
            if not opened and codec_name:
                logger.warning(
                    "FFmpegRtspCapture[%s]: [CUDA] retry with hevc_cuvid/h264_cuvid",
                    self._label,
                )
                for vf_var in cuda_bgr_vf_variants(out_w, out_h, scaled=scaled):
                    if opened:
                        break
                    opened = self._spawn_decode(
                        ffmpeg_bin,
                        out_w,
                        out_h,
                        scaled=scaled,
                        use_cuda=True,
                        codec_name=codec_name,
                        explicit_cuvid=True,
                        vf_variant=vf_var,
                    )
                    if opened:
                        decode = f"cuda_cuvid_{vf_var}:{codec_name}"
                        break
            if not opened:
                self._last_error = (
                    self._last_error
                    or "FFmpeg CUDA decode failed (no CPU fallback — fix NVDEC/URL/GPU load)"
                )
                self.decode_backend = "cuda_failed"
                logger.error(
                    "FFmpegRtspCapture[%s]: [CUDA FAILED] %s",
                    self._label,
                    self._last_error,
                )
                self._release_slot()
                return
        else:
            opened = self._spawn_decode(ffmpeg_bin, out_w, out_h, scaled=scaled, use_cuda=False)
            decode = "cpu_ffmpeg"
            if not opened:
                self.decode_backend = "cpu_failed"
                self._release_slot()
                return

        self.decode_backend = decode
        ok_tag = "[CUDA OK]" if want_cuda else "[CPU OK]"
        _log = logger.debug if self._label == "thumb" else logger.info
        if scaled:
            _log(
                "FFmpegRtspCapture[%s]: %s %s %dx%d → %dx%d ← %s",
                self._label,
                ok_tag,
                decode,
                src_w,
                src_h,
                out_w,
                out_h,
                self._url[:72],
            )
        else:
            _log(
                "FFmpegRtspCapture[%s]: %s %s %dx%d ← %s",
                self._label,
                ok_tag,
                decode,
                out_w,
                out_h,
                self._url[:72],
            )

    def isOpened(self) -> bool:
        if not self._opened or self._proc is None:
            return False
        return self._proc.poll() is None

    def _reshape_raw(self, raw: bytes) -> tuple[bool, np.ndarray | None]:
        n = len(raw)
        if n < self._w * 32 * 3 or n % 3 != 0:
            return False, None
        pix = n // 3
        if pix == self._w * self._h:
            # np.array() always returns a writable copy (np.frombuffer returns read-only)
            fr = np.frombuffer(raw, dtype=np.uint8).reshape((self._h, self._w, 3))
            return True, np.array(fr)
        if self._w > 0 and pix % self._w == 0:
            h = pix // self._w
            if h >= 32:
                self._h = int(h)
                self._frame_bytes = n
                fr = np.frombuffer(raw, dtype=np.uint8).reshape((self._h, self._w, 3))
                return True, np.array(fr)
        return False, None

    def read(self) -> tuple[bool, np.ndarray | None]:
        proc = self._proc
        if proc is None or proc.stdout is None or not self.isOpened():
            return False, None
        need = self._frame_bytes
        if self._initial_buf:
            buf = bytearray(self._initial_buf)
            self._initial_buf = b""
        else:
            buf = bytearray()
        deadline = time.time() + _READ_TIMEOUT_SEC
        try:
            while len(buf) < need and time.time() < deadline:
                if proc.poll() is not None:
                    break
                chunk = proc.stdout.read(min(262144, need - len(buf)))
                if not chunk:
                    time.sleep(0.02)
                    continue
                buf.extend(chunk)
        except Exception as e:
            self._last_error = str(e)
            return False, None
        if len(buf) < self._w * 32 * 3:
            with self._lock:
                if self._stderr_tail:
                    self._last_error = self._stderr_tail[-1]
            return False, None
        n = len(buf)
        rem = n % need
        if rem:
            buf = buf[rem:]
        if len(buf) < need:
            return False, None
        return self._reshape_raw(bytes(buf[-need:]))

    def grab(self) -> bool:
        """OpenCV-compatible: decode one frame (stored for retrieve())."""
        ok, fr = self.read()
        if ok and fr is not None:
            self._retrieve_buf = fr
            return True
        self._retrieve_buf = None
        return False

    def retrieve(self) -> tuple[bool, np.ndarray | None]:
        """Return frame from last grab()."""
        if self._retrieve_buf is not None:
            fr = self._retrieve_buf
            self._retrieve_buf = None
            return True, fr
        return False, None

    def read_fresh(self, flush_frames: int = 0) -> tuple[bool, np.ndarray | None]:
        """Read after optional buffer flush (for live RTSP)."""
        n = max(0, int(flush_frames))
        ok, fr = False, None
        for _ in range(n + 1):
            ok, fr = self.read()
            if not ok or fr is None:
                return False, None
        return ok, fr

    def _release_slot(self) -> None:
        global _live_slots_in_use, _thumb_slots_in_use
        if not self._slot_acquired:
            return
        if self._use_live_slot:
            with _live_slots_lock:
                _live_slots_in_use = max(0, _live_slots_in_use - 1)
        else:
            with _thumb_slots_lock:
                _thumb_slots_in_use = max(0, _thumb_slots_in_use - 1)
        self._slot_acquired = False

    def release(self) -> None:
        self._opened = False
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._stderr_t:
            self._stderr_t.join(timeout=1.5)
            self._stderr_t = None
        self._release_slot()

    def set(self, prop: int, value: float) -> bool:
        return False
