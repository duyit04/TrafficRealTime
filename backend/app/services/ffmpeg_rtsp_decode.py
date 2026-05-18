"""
Shared FFmpeg RTSP demuxer + CUDA decode flags for relay and BGR capture.

Relay (rtsp_relay) and burn-in capture must use the same hwaccel path so HEVC
does not corrupt on CPU software decode.
"""

from __future__ import annotations

from app.core.config import settings


def cuda_decode_enabled() -> bool:
    if not bool(getattr(settings, "RTSP_HWACCEL", False)):
        return False
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def rtsp_demuxer_flags(*, loglevel: str = "error") -> list[str]:
    return [
        "-hide_banner",
        "-loglevel",
        loglevel,
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "nobuffer+discardcorrupt",
        "-flags",
        "low_delay",
        "-err_detect",
        "ignore_err",
    ]


def rtsp_capture_timeout_flags() -> list[str]:
    # WinGet FFmpeg: RTSP demuxer uses -timeout (µs), not -stimeout/-rw_timeout.
    return ["-timeout", "8000000"]


def cuda_hwaccel_before_input(*, device_frames: bool = True) -> list[str]:
    dev = max(0, int(getattr(settings, "RTSP_CUDA_DEVICE", 0) or 0))
    extra = max(0, min(int(getattr(settings, "RTSP_CUDA_EXTRA_FRAMES", 16) or 16), 64))
    args = [
        "-hwaccel",
        "cuda",
        "-hwaccel_device",
        str(dev),
        "-extra_hw_frames",
        str(extra),
    ]
    if device_frames:
        args += ["-hwaccel_output_format", "cuda"]
    return args


def cuda_decoder_before_input(codec_name: str | None) -> list[str]:
    """Explicit NVDEC decoder — helps RTSP HEVC when generic hwaccel pick fails."""
    c = (codec_name or "").strip().lower()
    if c in {"hevc", "h265"}:
        return ["-c:v", "hevc_cuvid"]
    if c in {"h264", "avc"}:
        return ["-c:v", "h264_cuvid"]
    return []


def ffmpeg_cuda_required() -> bool:
    """
    When True (default while RTSP_HWACCEL + CUDA available): never fall back to CPU
    FFmpeg decode or OpenCV — failures stay visible as CUDA FAILED in logs/status.
    Set RTSP_FFMPEG_CUDA_REQUIRED=false only to allow CPU fallback for debugging.
    """
    if not bool(getattr(settings, "RTSP_FFMPEG_CUDA_REQUIRED", True)):
        return False
    return cuda_decode_enabled()


def bgr_pipe_vf(
    out_w: int,
    out_h: int,
    *,
    scaled: bool,
    cuda: bool,
    variant: str = "scale_cuda",
) -> str:
    """
    CUDA burn-in filter chains (all use NVDEC; no CPU decode).
    variant download_scale: NVDEC on GPU, hwdownload nv12, CPU scale (most compatible for BGR pipe).
    variant scale_cuda: scale on GPU then hwdownload nv12 → bgr24.
    variant sysmem: NVDEC, frames in system memory — no hwdownload filter.
    """
    if cuda:
        if variant == "sysmem":
            if scaled:
                return f"scale={out_w}:{out_h}:flags=fast_bilinear,format=bgr24"
            return "format=bgr24"
        if scaled and variant == "scale_cuda":
            return f"scale_cuda={out_w}:{out_h},hwdownload,format=nv12,format=bgr24"
        if scaled:
            return f"hwdownload,format=nv12,scale={out_w}:{out_h},format=bgr24"
        return "hwdownload,format=nv12,format=bgr24"
    if scaled:
        return f"scale={out_w}:{out_h}:flags=fast_bilinear,format=bgr24"
    return "format=bgr24"


def cuda_bgr_vf_variants(out_w: int, out_h: int, *, scaled: bool) -> list[str]:
    """Ordered CUDA vf attempts — still no CPU decoder fallback."""
    if not scaled:
        return ["default"]
    # scale_cuda first when fixed (nv12→bgr24): avoids full 4K hwdownload; then fallbacks.
    return ["scale_cuda", "download_scale", "sysmem"]
