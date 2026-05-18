/**
 * useWebSocket – JSON stats/detections channel (H264 video uses separate WS).
 */

import { useState, useEffect, useRef, useCallback } from 'react';

export interface UseWebSocketOptions {
  url: string;
  enabled: boolean;
  onMessage: (data: unknown) => void;
  maxRetries?: number;
  retryDelay?: number;
}

export interface UseWebSocketResult {
  connected: boolean;
  disconnect: () => void;
}

export function useWebSocket({
  url,
  enabled,
  onMessage,
  maxRetries = 8,
  retryDelay = 2000,
}: UseWebSocketOptions): UseWebSocketResult {
  const [connected, setConnected] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const enabledRef = useRef(enabled);
  const onMessageRef = useRef(onMessage);

  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  const disconnect = useCallback(() => {
    clearTimeout(retryTimerRef.current);
    if (wsRef.current) {
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
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          if (typeof event.data !== 'string') return;
          const data = JSON.parse(event.data) as Record<string, unknown>;
          if ('ping' in data) return;
          onMessageRef.current(data);
        } catch {
          // ignore malformed
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!enabledRef.current) return;
        retryCountRef.current += 1;
        if (retryCountRef.current <= maxRetries) {
          retryTimerRef.current = setTimeout(connect, retryDelay);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch (err) {
      console.error('[useWebSocket] failed to create WebSocket:', err);
    }
  }, [url, maxRetries, retryDelay]);

  useEffect(() => {
    if (!enabled) {
      disconnect();
      retryCountRef.current = 0;
      return;
    }
    if (typeof WebSocket === 'undefined') return;
    connect();
    return () => {
      disconnect();
    };
  }, [enabled, connect, disconnect]);

  return { connected, disconnect };
}
