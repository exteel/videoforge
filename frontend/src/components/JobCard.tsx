import { useState } from 'react'
import { type Job, api } from '../api'
import { useWebSocket } from '../hooks/useWebSocket'

const STEPS = ['', 'Script', 'Images + Voices', 'Subtitles', 'Video', 'Thumbnail', 'Metadata']

const STATUS_COLOR: Record<string, string> = {
  queued:    'bg-yellow-900 text-yellow-300',
  running:   'bg-blue-900 text-blue-300',
  done:      'bg-green-900 text-green-300',
  failed:    'bg-red-900 text-red-300',
  cancelled: 'bg-gray-700 text-gray-300',
}

interface Props {
  job: Job
  onRefresh: () => void
}

export function JobCard({ job, onRefresh }: Props) {
  const isActive = job.status === 'running' || job.status === 'queued'
  const { events } = useWebSocket(isActive ? job.job_id : null)
  const [expanded, setExpanded] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  // Derive live step from WS events (fall back to snapshot)
  const liveStep = events.reduce<number>((acc, e) => {
    if (e.type === 'step_start' && typeof e.step === 'number') return e.step
    return acc
  }, job.step)

  const allLogs = [
    ...job.logs,
    ...events.filter((e) => e.type === 'log').map((e) => String(e.message)),
  ]

  const liveStatus = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const t = events[i].type
      if (['done', 'error', 'cancelled', 'running'].includes(t)) return t
    }
    return job.status
  })()

  async function handleCancel() {
    setCancelling(true)
    try {
      await api.jobs.cancel(job.job_id)
      onRefresh()
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-xs text-gray-400">{job.job_id}</span>
            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${STATUS_COLOR[liveStatus] ?? 'bg-gray-700 text-gray-300'}`}>
              {liveStatus}
            </span>
            <span className="text-xs text-gray-500">{job.kind}</span>
            <span className="text-xs text-gray-500">{job.quality}</span>
          </div>
          <div className="text-sm text-white mt-1 truncate">{job.source}</div>
          <div className="text-xs text-gray-400">{job.channel}</div>
        </div>
        <div className="flex gap-2 shrink-0">
          {isActive && (
            <button
              onClick={handleCancel}
              disabled={cancelling}
              className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 text-red-200 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-xs px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-300"
          >
            {expanded ? 'Hide' : 'Logs'}
          </button>
        </div>
      </div>

      {/* Step progress bar (pipeline only) */}
      {job.kind === 'pipeline' && (liveStatus === 'running' || liveStatus === 'done') && (
        <div className="space-y-1">
          <div className="flex justify-between text-xs text-gray-400">
            <span>{liveStatus === 'running' ? STEPS[liveStep] || '…' : 'Done'}</span>
            <span>{liveStep}/6</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-1.5">
            <div
              className="bg-blue-500 h-1.5 rounded-full transition-all duration-500"
              style={{ width: `${Math.round((liveStep / 6) * 100)}%` }}
            />
          </div>
        </div>
      )}

      {/* Error */}
      {job.error && (
        <div className="text-xs text-red-300 bg-red-950 rounded p-2 font-mono break-all">
          {job.error}
        </div>
      )}

      {/* Elapsed */}
      {job.elapsed != null && (
        <div className="text-xs text-gray-400">
          {job.elapsed.toFixed(1)}s elapsed
          {job.db_video_id != null && ` · DB #${job.db_video_id}`}
        </div>
      )}

      {/* Log tail */}
      {expanded && allLogs.length > 0 && (
        <div className="bg-gray-900 rounded p-2 max-h-48 overflow-y-auto font-mono text-xs text-gray-300 space-y-0.5">
          {allLogs.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      )}
    </div>
  )
}
