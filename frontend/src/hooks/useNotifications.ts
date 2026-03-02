/**
 * useNotifications — thin wrapper around the Web Notifications API.
 *
 * Usage:
 *   const { permission, requestPermission, notify } = useNotifications()
 *
 *   // Ask user once
 *   await requestPermission()
 *
 *   // Fire a notification (only if permission is 'granted')
 *   notify('Title', 'Body text', { tag: 'unique-tag', onlyWhenHidden: true })
 */

import { useCallback, useState } from 'react'

export type NotificationPermission = 'default' | 'granted' | 'denied'

export interface NotifyOptions {
  /** Deduplicate notifications with the same tag */
  tag?: string
  /** Icon URL (defaults to /favicon.ico) */
  icon?: string
  /**
   * When true, only fires if the document is hidden or not focused.
   * Prevents noisy popups when the user is already looking at the tab.
   * Default: false (always fire if granted)
   */
  onlyWhenHidden?: boolean
}

const supported = typeof Notification !== 'undefined'

export function useNotifications() {
  const [permission, setPermission] = useState<NotificationPermission>(
    supported ? Notification.permission : 'denied'
  )

  /** Ask the browser for notification permission (call on user gesture). */
  const requestPermission = useCallback(async (): Promise<NotificationPermission> => {
    if (!supported) return 'denied'
    const p = await Notification.requestPermission()
    setPermission(p)
    return p
  }, [])

  /**
   * Show a system notification.
   * No-op when:
   *   - Notifications not supported
   *   - Permission not granted
   *   - onlyWhenHidden=true and the tab is currently visible/focused
   */
  const notify = useCallback(
    (title: string, body: string, options: NotifyOptions = {}) => {
      if (!supported) return
      // Always read from global — avoids stale closure
      if (Notification.permission !== 'granted') return

      const { onlyWhenHidden = false, tag, icon = '/favicon.ico' } = options

      if (onlyWhenHidden) {
        const isVisible = document.visibilityState === 'visible' && document.hasFocus()
        if (isVisible) return
      }

      const n = new Notification(title, { body, tag, icon, requireInteraction: false })
      n.onclick = () => {
        window.focus()
        n.close()
      }
    },
    [] // stable — reads Notification.permission at call time
  )

  return { permission, requestPermission, notify }
}
