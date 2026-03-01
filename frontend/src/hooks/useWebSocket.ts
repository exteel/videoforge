import { useEffect, useRef, useState } from 'react'

export interface WsEvent {
  type: string
  [key: string]: unknown
}

export function useWebSocket(jobId: string | null) {
  const [events, setEvents] = useState<WsEvent[]>([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    if (!jobId) return

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${jobId}`)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => setConnected(false)
    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data) as WsEvent
        if (event.type === 'ping') return
        setEvents((prev) => [...prev, event])
      } catch {
        // ignore malformed frames
      }
    }

    return () => {
      ws.close()
      wsRef.current = null
      setConnected(false)
    }
  }, [jobId])

  return { events, connected }
}
