"""
Backward-compatible aliases for H264 MPEG-TS pipes (BGR burn-in + NVENC).

Prefer importing from app.services.ffmpeg_h264_bgr_pipe_service in new code.
"""

from app.services.ffmpeg_h264_bgr_pipe_service import (
    h264_bgr_primary as ffmpeg_relay_service,
    h264_bgr_companion as ffmpeg_relay_companion_service,
    h264_bgr_extra2 as ffmpeg_relay_extra2_service,
    h264_bgr_extra3 as ffmpeg_relay_extra3_service,
)

__all__ = [
    "ffmpeg_relay_service",
    "ffmpeg_relay_companion_service",
    "ffmpeg_relay_extra2_service",
    "ffmpeg_relay_extra3_service",
]
