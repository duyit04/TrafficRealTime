"""
WebSocket routes – real-time frame streaming to frontend clients.

Endpoint: GET /ws/stream
  - Upgrades HTTP to WebSocket
  - Receives broadcast frames from StreamService worker thread
  - Supports multiple simultaneous clients
  - Client just connects and listens; no need to send requests per frame
"""

from __future__ import annotations
import asyncio
import struct
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logger import logger

router = APIRouter(tags=["websocket"])

WS_BINARY_MAGIC = b"TMWS"


def pack_frame_message(header: dict[str, Any], jpeg_bytes: bytes) -> bytes:
    """
    Pack a single binary WS message:
      magic(4) + json_len(uint32 LE) + json_utf8 + jpeg_bytes
    """
    import json as _json

    header_bytes = _json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return WS_BINARY_MAGIC + struct.pack("<I", len(header_bytes)) + header_bytes + jpeg_bytes


class ConnectionManager:
    """
    Manages active WebSocket connections and broadcasts messages to all clients.
    Thread-safe: broadcast() is called from the async event loop only.
    """

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            pass
        logger.info("WS: client connected (total=%d)", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
        logger.info("WS: client disconnected (total=%d)", len(self.active))

    async def broadcast(self, data: str) -> None:
        """Send text data to all connected clients; remove any that have closed."""
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_bytes(self, data: bytes) -> None:
        """Send binary data to all connected clients; remove any that have closed."""
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_bytes_threadsafe(self, data: bytes) -> None:
        """
        Best-effort thread-safe byte broadcast from non-async threads.
        Uses per-send tasks to avoid blocking worker threads.
        """
        if not self.active:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_bytes(data)))
        except Exception:
            pass

    @property
    def has_clients(self) -> bool:
        return len(self.active) > 0


# Module-level singleton — imported by StreamService to push frames
ws_manager = ConnectionManager()
ws_companion_manager = ConnectionManager()
ws_h264_manager = ConnectionManager()
ws_h264_companion_manager = ConnectionManager()
ws_h264_extra2_manager = ConnectionManager()
ws_h264_extra3_manager = ConnectionManager()


def get_h264_manager(slot: str) -> ConnectionManager:
    s = (slot or "primary").strip().lower()
    if s in {"primary", "1", "main"}:
        return ws_h264_manager
    if s in {"companion", "2", "cam2"}:
        return ws_h264_companion_manager
    if s in {"extra2", "3", "cam3"}:
        return ws_h264_extra2_manager
    if s in {"extra3", "4", "cam4"}:
        return ws_h264_extra3_manager
    # Unknown slot -> keep backward compatibility on primary channel.
    return ws_h264_manager


@router.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket) -> None:
    """
    WebSocket endpoint for live frame streaming.
    Client connects once; frames are pushed by the backend worker as they arrive.
    """
    await ws_manager.connect(ws)
    try:
        # Keep connection alive; we only send, never expect messages from client.
        # If client sends anything (e.g. ping), just ignore it.
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a lightweight ping to detect dead connections
                try:
                    await ws.send_text('{"ping":1}')
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS: connection error: %s", e)
    finally:
        ws_manager.disconnect(ws)


@router.websocket("/ws/companion")
async def websocket_companion(ws: WebSocket) -> None:
    """WebSocket endpoint for companion (2nd RTSP) frames."""
    await ws_companion_manager.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text('{"ping":1}')
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS(companion): connection error: %s", e)
    finally:
        ws_companion_manager.disconnect(ws)


@router.websocket("/ws/stream-h264")
async def websocket_stream_h264(ws: WebSocket) -> None:
    """
    WebSocket endpoint for MPEG-TS(H264) live stream.
    Byte chunks are produced from the OpenCV/YOLO frame path (BGR burn-in + NVENC).
    """
    mgr = ws_h264_manager
    await mgr.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text('{"ping":1}')
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS(h264): connection error: %s", e)
    finally:
        mgr.disconnect(ws)


@router.websocket("/ws/stream-h264/{slot}")
async def websocket_stream_h264_slot(ws: WebSocket, slot: str) -> None:
    """
    Slot-based H264 channel:
      - primary (camera 1)
      - companion (camera 2)
      - extra2 (camera 3)
      - extra3 (camera 4)
    """
    mgr = get_h264_manager(slot)
    await mgr.connect(ws)
    try:
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text('{"ping":1}')
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS(h264:%s): connection error: %s", slot, e)
    finally:
        mgr.disconnect(ws)
