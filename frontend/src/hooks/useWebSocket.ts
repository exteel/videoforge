import { useEffect, useRef, useState } from 'react'

export interface WsEvent {
  type: string
  [key: string]: unknown
}

const TERMINAL_TYPES = new Set(['done', 'error', 'cancelled'])

export function useWebSocket(jobId: string | null) {
  const [events, setEvents] = useState<WsEvent[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const retryCount = useRef(0)
  const cancelledRef = useRef(false)

  useEffect(() => {
    if (!jobId) return

    cancelledRef.current = false
    retryCount.current = 0

    function connect() {
      if (cancelledRef.current) return

      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${jobId}`)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        retryCount.current = 0
      }

      ws.onclose = () => {
        setConnected(false)
        if (!cancelledRef.current) {
          // Exponential backoff: 1s → 2s → 4s → 8s → 16s → 30s max
          const delay = Math.min(1000 * Math.pow(2, retryCount.current), 30_000)
          retryCount.current += 1
          retryRef.current = setTimeout(connect, delay)
        }
      }

      ws.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data) as WsEvent
          if (event.type === 'ping') return
          setEvents((prev) => [...prev, event])
          // Stop reconnecting when job reaches a terminal state
          if (TERMINAL_TYPES.has(event.type)) {
            cancelledRef.current = true
          }
        } catch {
          // ignore malformed frames
        }
      }

      ws.onerror = () => {
        // onclose fires right after, which handles reconnect
      }
    }

    connect()

    return () => {
      cancelledRef.current = true
      if (retryRef.current) {
        clearTimeout(retryRef.current)
        retryRef.current = null
      }
      wsRef.current?.close()
      wsRef.current = null
      setConnected(false)
    }
  }, [jobId])

  return { events, connected }
}
