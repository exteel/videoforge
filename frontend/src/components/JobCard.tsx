import { useState, useEffect } from 'react'
import { type Job, api } from '../api'
import { useWebSocket } from '../hooks/useWebSocket'

const STEPS = ['', 'Script', 'Images + Voices', 'Subtitles', 'Video', 'Thumbnail', 'Metadata']

// Estimated seconds per step (based on typical runs)
const STEP_EST_SEC = [0, 60, 120, 15, 30, 30, 10]

const STATUS_COLOR: Record<string, string> = {
  queued:    'bg-yellow-900 text-yellow-300',
  running:   'bg-blue-900 text-blue-300',
  done:      'bg-green-900 text-green-300',
  failed:    'bg-red-900 text-red-300',
  cancelled: 'bg-gray-700 text-gray-300',
}

function fmtSec(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return m > 0 ? `${m}м ${s}с` : `${s}с`
}

/** Simple ETA: average time per completed step × remaining steps */
function calcETA(step: number, elapsedSec: number): string | null {
  if (step < 1 || elapsedSec <= 0) return null
  const avgPerStep = elapsedSec / step
  const remaining = avgPerStep * (6 - step)
  if (remaining <= 0) return 'завершення…'
  return `ETA ~${fmtSec(remaining)}`
}

/** Estimate % progress within the current step using historical step durations */
function calcPct(step: number, elapsedSec: number): number {
  if (step > 6) return 100
  if (step < 1) return 0
  // Time estimated to have been spent in previous steps
  const prevEst = STEP_EST_SEC.slice(1, step).reduce((a, b) => a + b, 0)
  const timeInStep = elapsedSec - prevEst
  const curEst = STEP_EST_SEC[step] || 30
  const stepFrac = Math.min(1, Math.max(0, timeInStep / curEst))
  // Each step is 1/6 of total; fraction within current step is bonus
  return Math.min(100, ((step - 1 + stepFrac) / 6) * 100)
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
  const [liveSec, setLiveSec] = useState<number | null>(null)

  // Live elapsed timer — ticks every second while job is active
  useEffect(() => {
    if (!isActive || !job.started_at) {
      setLiveSec(null)
      return
    }
    const calc = () => {
      const started = new Date(job.started_at!).getTime()
      setLiveSec(Math.floor((Date.now() - started) / 1000))
    }
    calc()
    const id = setInterval(calc, 1000)
    return () => clearInterval(id)
  }, [isActive, job.started_at])

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

  const elapsedSec = liveSec ?? job.elapsed ?? 0
  const pct = liveStatus === 'done' ? 100 : calcPct(liveStep, elapsedSec)
  const eta = liveStatus === 'running' ? calcETA(liveStep, elapsedSec) : null

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
            <span className="font-medium">
              {liveStatus === 'done'
                ? '✓ Done'
                : `Step ${liveStep}/6 — ${STEPS[liveStep] || '…'}`}
            </span>
            <span className="tabular-nums">{Math.round(pct)}%</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-2">
            <div
              className={`h-2 rounded-full transition-all duration-500 ${liveStatus === 'done' ? 'bg-green-500' : 'bg-blue-500'}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          {/* Step indicator dots */}
          <div className="flex justify-between mt-1">
            {STEPS.slice(1).map((name, i) => {
              const stepN = i + 1
              const done = liveStatus === 'done' || stepN < liveStep
              const active = stepN === liveStep && liveStatus === 'running'
              return (
                <div key={stepN} className="flex flex-col items-center gap-0.5" style={{ width: '14%' }}>
                  <div className={`w-2 h-2 rounded-full ${done ? 'bg-green-500' : active ? 'bg-blue-400 animate-pulse' : 'bg-gray-600'}`} />
                  <span className="text-[9px] text-gray-500 text-center leading-tight hidden sm:block">{name}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Batch progress (batch kind) */}
      {job.kind === 'batch' && liveStatus === 'running' && (
        <div className="space-y-1">
          <div className="text-xs text-gray-400">Batch running…</div>
          <div className="w-full bg-gray-700 rounded-full h-1.5">
            <div className="bg-purple-500 h-1.5 rounded-full animate-pulse" style={{ width: '100%' }} />
          </div>
        </div>
      )}

      {/* Timing row */}
      <div className="flex items-center gap-4 text-xs text-gray-400 tabular-nums">
        {isActive && liveSec !== null && (
          <span>⏱ {fmtSec(liveSec)}</span>
        )}
        {eta && <span className="text-blue-400">{eta}</span>}
        {!isActive && job.elapsed != null && (
          <span>⏱ {fmtSec(job.elapsed)}</span>
        )}
        {job.db_video_id != null && (
          <span className="text-gray-500">DB #{job.db_video_id}</span>
        )}
      </div>

      {/* Error */}
      {job.error && (
        <div className="text-xs text-red-300 bg-red-950 rounded p-2 font-mono break-all">
          ✗ {job.error}
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
      {expanded && allLogs.length === 0 && (
        <div className="text-xs text-gray-500 italic">No logs yet…</div>
      )}
    </div>
  )
}
