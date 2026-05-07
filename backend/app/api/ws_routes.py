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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logger import logger

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """
    Manages active WebSocket connections and broadcasts messages to all clients.
    Thread-safe: broadcast() is called from the async event loop only.
    """

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
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

    @property
    def has_clients(self) -> bool:
        return len(self.active) > 0


# Module-level singleton — imported by StreamService to push frames
ws_manager = ConnectionManager()
ws_companion_manager = ConnectionManager()


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
