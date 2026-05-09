/**
 * useWebSocket – manages a WebSocket connection for live frame streaming.
 *
 * - Connects to the given URL when `enabled` is true
 * - Calls `onMessage` for every parsed JSON message
 * - Auto-reconnects on close/error (up to maxRetries times, with retryDelay ms between attempts)
 * - After maxRetries failures, sets `usingFallback = true` so caller can switch to HTTP polling
 * - Disconnects cleanly when `enabled` becomes false or component unmounts
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import type { FramePayload } from '../types/detection';

const WS_MAGIC = 'TMWS';

function bytesToAscii(bytes: Uint8Array, start: number, len: number): string {
  let out = '';
  for (let i = 0; i < len; i++) out += String.fromCharCode(bytes[start + i] ?? 0);
  return out;
}

function parseBinaryFrame(buf: ArrayBuffer): any | null {
  const bytes = new Uint8Array(buf);
  if (bytes.length < 8) return null;
  const magic = bytesToAscii(bytes, 0, 4);
  if (magic !== WS_MAGIC) return null;
  const view = new DataView(buf);
  const jsonLen = view.getUint32(4, true);
  const headerStart = 8;
  const headerEnd = headerStart + jsonLen;
  if (headerEnd > bytes.length) return null;
  const headerBytes = bytes.slice(headerStart, headerEnd);
  const headerText = new TextDecoder('utf-8').decode(headerBytes);
  const header = JSON.parse(headerText);
  const jpegBytes = bytes.slice(headerEnd);
  const blob = new Blob([jpegBytes], { type: 'image/jpeg' });
  return { ...header, frame: null, frame_blob: blob } satisfies Partial<FramePayload>;
}

export interface UseWebSocketOptions {
  /** WebSocket URL, e.g. "ws://localhost:8000/ws/stream" */
  url: string;
  /** Whether the connection should be active */
  enabled: boolean;
  /** Called for every valid JSON message received */
  onMessage: (data: any) => void;
  /** Max reconnect attempts before falling back to HTTP polling (default: 5) */
  maxRetries?: number;
  /** Delay between reconnect attempts in ms (default: 2000) */
  retryDelay?: number;
}

export interface UseWebSocketResult {
  connected: boolean;
  usingFallback: boolean;
  disconnect: () => void;
}

export function useWebSocket({
  url,
  enabled,
  onMessage,
  maxRetries = 5,
  retryDelay = 2000,
}: UseWebSocketOptions): UseWebSocketResult {
  const [connected, setConnected] = useState(false);
  const [usingFallback, setUsingFallback] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const enabledRef = useRef(enabled);
  const onMessageRef = useRef(onMessage);

  // Keep refs in sync so closures always see latest values
  useEffect(() => { enabledRef.current = enabled; }, [enabled]);
  useEffect(() => { onMessageRef.current = onMessage; }, [onMessage]);

  const disconnect = useCallback(() => {
    clearTimeout(retryTimerRef.current);
    if (wsRef.current) {
      // Prevent reconnect on intentional close
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  const connect = useCallback(() => {
    if (!enabledRef.current) return;
    if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(url);
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      ws.onopen = () => {
        retryCountRef.current = 0;
        setConnected(true);
        setUsingFallback(false);
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          if (typeof event.data === 'string') {
            const data = JSON.parse(event.data as string) as any;
            // Ignore keepalive pings from server
            if ('ping' in data) return;
            onMessageRef.current(data);
            return;
          }
          if (event.data instanceof ArrayBuffer) {
            const parsed = parseBinaryFrame(event.data);
            if (parsed) onMessageRef.current(parsed);
            return;
          }
          if (event.data instanceof Blob) {
            // Should not happen with binaryType='arraybuffer', but handle anyway.
            void event.data.arrayBuffer().then((ab) => {
              const parsed = parseBinaryFrame(ab);
              if (parsed) onMessageRef.current(parsed);
            });
          }
        } catch {
          // Malformed message — ignore
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!enabledRef.current) return;

        retryCountRef.current += 1;
        if (retryCountRef.current <= maxRetries) {
          retryTimerRef.current = setTimeout(connect, retryDelay);
        } else {
          console.warn(
            `[useWebSocket] ${maxRetries} reconnect attempts failed — switching to HTTP polling fallback`
          );
          setUsingFallback(true);
        }
      };

      ws.onerror = () => {
        // onerror is always followed by onclose, so let onclose handle retry logic
        ws.close();
      };
    } catch (err) {
      console.error('[useWebSocket] failed to create WebSocket:', err);
      setUsingFallback(true);
    }
  }, [url, maxRetries, retryDelay]);

  useEffect(() => {
    if (!enabled) {
      disconnect();
      retryCountRef.current = 0;
      setUsingFallback(false);
      return;
    }

    // Check WebSocket support
    if (typeof WebSocket === 'undefined') {
      setUsingFallback(true);
      return;
    }

    connect();

    return () => {
      disconnect();
    };
  }, [enabled, connect, disconnect]);

  return { connected, usingFallback, disconnect };
}
