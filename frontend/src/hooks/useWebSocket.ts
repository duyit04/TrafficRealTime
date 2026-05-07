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
      wsRef.current = ws;

      ws.onopen = () => {
        retryCountRef.current = 0;
        setConnected(true);
        setUsingFallback(false);
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data as string) as any;
          // Ignore keepalive pings from server
          if ('ping' in data) return;
          onMessageRef.current(data);
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
