import { useState, useEffect, useRef } from 'react'
import { type Job, api } from '../api'
import { useWebSocket } from '../hooks/useWebSocket'
import { useNotifications } from '../hooks/useNotifications'

const STEPS = ['', 'Script', 'Images + Voices', 'Subtitles', 'Video', 'Thumbnail', 'Metadata']

// Estimated seconds per step (for time-based animation fallback)
const STEP_EST_SEC = [0, 60, 180, 15, 60, 60, 15]

// Global % range per step — must match STEP_WEIGHTS in pipeline.py
//   [start%, end%] for steps 0..6
const STEP_PCT_RANGE: [number, number][] = [
  [0,  0],    // 0: unused
  [0,  15],   // 1: Script
  [15, 55],   // 2: Images + Voices
  [55, 60],   // 3: Subtitles
  [60, 80],   // 4: Video
  [80, 93],   // 5: Thumbnail
  [93, 100],  // 6: Metadata
]

const STATUS_COLOR: Record<string, string> = {
  queued:         'bg-yellow-900 text-yellow-300',
  running:        'bg-blue-900 text-blue-300',
  waiting_review: 'bg-amber-800 text-amber-300',
  done:           'bg-green-900 text-green-300',
  failed:         'bg-red-900 text-red-300',
  cancelled:      'bg-gray-700 text-gray-300',
}

const REVIEW_LABEL: Record<string, { icon: string; title: string; hint: string }> = {
  script: { icon: '📋', title: 'Review Script', hint: 'Script generated — approve to start image & voice generation' },
  images: { icon: '🖼', title: 'Review Images', hint: 'Images validated — approve to continue to video compilation' },
}

const BLOCK_TYPE_COLOR: Record<string, string> = {
  intro:   'bg-blue-800 text-blue-200',
  section: 'bg-gray-700 text-gray-300',
  cta:     'bg-purple-800 text-purple-200',
  outro:   'bg-green-800 text-green-200',
}

function fmtSec(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return m > 0 ? `${m}м ${s}с` : `${s}с`
}

/** ETA from real pct + elapsed time */
function calcETAfromPct(pct: number, elapsedSec: number): string | null {
  if (pct <= 0 || elapsedSec <= 0) return null
  if (pct >= 99) return 'завершення…'
  const totalEst = elapsedSec / (pct / 100)
  const remaining = totalEst - elapsedSec
  if (remaining <= 5) return 'завершення…'
  return `ETA ~${fmtSec(remaining)}`
}

/**
 * Time-based pct estimate within the current step's expected % range.
 * Fills 95% of the step range based on elapsed time so the bar
 * always moves forward even without WS sub-progress events.
 */
function calcPct(step: number, elapsedSec: number): number {
  if (step < 1) return 0
  if (step > 6) return 100
  const [stepStart, stepEnd] = STEP_PCT_RANGE[step]
  // Seconds estimated for all previous steps
  const prevEst = STEP_EST_SEC.slice(1, step).reduce((a, b) => a + b, 0)
  const timeInStep = Math.max(0, elapsedSec - prevEst)
  const curEst = STEP_EST_SEC[step] || 30
  // Cap at 95% of the step range — backend step_done event will push it to 100%
  const frac = Math.min(0.95, timeInStep / curEst)
  return stepStart + frac * (stepEnd - stepStart)
}

interface Props {
  job: Job
  onRefresh: () => void
}

export function JobCard({ job, onRefresh }: Props) {
  const isActive = ['running', 'queued', 'waiting_review'].includes(job.status)
  const { events } = useWebSocket(isActive ? job.job_id : null)
  const [expanded, setExpanded] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [approving, setApproving] = useState(false)
  const [liveSec, setLiveSec] = useState<number | null>(null)

  // ── Browser notifications ──────────────────────────────────────────────────
  const { notify } = useNotifications()
  // Track how many events we've already evaluated so we only notify on NEW ones
  const notifyPtrRef = useRef(0)

  useEffect(() => {
    const newEvents = events.slice(notifyPtrRef.current)
    notifyPtrRef.current = events.length

    for (const e of newEvents) {
      const label = job.source || job.job_id
      if (e.type === 'review_required') {
        const stage = e.stage === 'script' ? 'Сценарій' : 'Зображення'
        notify('VideoForge — Потрібне ревью', `${label}: ${stage} готовий до перевірки`, {
          tag: `review-${job.job_id}`,
          onlyWhenHidden: true,
        })
      } else if (e.type === 'done') {
        notify('VideoForge — Готово ✓', `${label}: відео згенеровано`, {
          tag: `done-${job.job_id}`,
          onlyWhenHidden: true,
        })
      } else if (e.type === 'error') {
        notify('VideoForge — Помилка', `${label}: ${String(e.message ?? 'Pipeline error')}`.slice(0, 120), {
          tag: `error-${job.job_id}`,
          onlyWhenHidden: true,
        })
      }
    }
  }, [events, job.source, job.job_id, notify])

  // Derive current status from WS events (newest-first scan)
  const liveStatus = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      const t = e.type
      if (t === 'done') return 'done'
      if (t === 'error') return 'failed'
      if (t === 'cancelled') return 'cancelled'
      if (t === 'review_required') return 'waiting_review'
      if (t === 'review_approved') return 'running'
      if (t === 'status' && typeof e.status === 'string') return e.status as string
    }
    return job.status
  })()

  // Extract current review stage + data (review_required sets, review_approved clears)
  const { liveReviewStage, liveReviewData } = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.type === 'review_required') return {
        liveReviewStage: e.stage as string,
        liveReviewData: (e.data ?? {}) as Record<string, unknown>,
      }
      if (e.type === 'review_approved') return { liveReviewStage: null, liveReviewData: null }
    }
    return {
      liveReviewStage: liveStatus === 'waiting_review' ? job.review_stage : null,
      liveReviewData: null,
    }
  })()

  // Live elapsed timer — ticks every second while job is not terminal
  useEffect(() => {
    const terminal = ['done', 'failed', 'cancelled']
    if (terminal.includes(liveStatus) || !job.started_at) {
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
  }, [liveStatus, job.started_at])

  // Derive live step from WS events (fall back to snapshot)
  const liveStep = events.reduce<number>((acc, e) => {
    if (e.type === 'step_start' && typeof e.step === 'number') return e.step
    return acc
  }, job.step)

  const allLogs = [
    ...job.logs,
    ...events.filter((e) => e.type === 'log').map((e) => String(e.message)),
  ]

  const elapsedSec = liveSec ?? job.elapsed ?? 0

  // Real pct + message: scan WS events newest-first for sub_progress / step events with pct field
  const { livePctFromWS, liveSubMsg } = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (
        typeof e.pct === 'number' &&
        (e.type === 'sub_progress' || e.type === 'step_start' || e.type === 'step_done')
      ) {
        return {
          livePctFromWS: e.pct as number,
          liveSubMsg: e.type === 'sub_progress' && e.message ? String(e.message) : null,
        }
      }
    }
    return { livePctFromWS: null, liveSubMsg: null }
  })()

  // Best known real pct (from WS events or last polled snapshot)
  const realPct = livePctFromWS !== null ? livePctFromWS : job.pct
  // Time-based estimate within current step's expected range
  const timePct = calcPct(liveStep, elapsedSec)

  // Take the MAXIMUM of real and time-based: bar always moves forward.
  // realPct=0 at step boundary → timePct fills in smooth animation.
  // When real pct arrives (step_done / sub_progress), it overrides if higher.
  const pct =
    liveStatus === 'done' ? 100
    : Math.max(realPct, timePct)

  const eta = liveStatus === 'running' ? calcETAfromPct(pct, elapsedSec) : null

  async function handleCancel() {
    setCancelling(true)
    try {
      await api.jobs.cancel(job.job_id)
      onRefresh()
    } finally {
      setCancelling(false)
    }
  }

  async function handleApprove() {
    if (!liveReviewStage) return
    setApproving(true)
    try {
      await api.jobs.approve(job.job_id, liveReviewStage)
    } catch (err) {
      console.error('Approve failed:', err)
    } finally {
      setApproving(false)
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
      {job.kind === 'pipeline' && (liveStatus === 'running' || liveStatus === 'waiting_review' || liveStatus === 'done') && (
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
          {/* Sub-step message (e.g. "Block 3/10", "Concat done") */}
          {liveSubMsg && liveStatus === 'running' && (
            <div className="text-[10px] text-gray-500 italic">{liveSubMsg}</div>
          )}
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

      {/* Review checkpoint banner */}
      {liveStatus === 'waiting_review' && liveReviewStage && (() => {
        const info = REVIEW_LABEL[liveReviewStage] ?? { icon: '⏸', title: 'Review Required', hint: 'Waiting for approval' }
        const d = liveReviewData ?? {}

        // Script-specific data
        const scriptBlocks = d.blocks as { id: string; type: string; narration: string }[] | undefined
        const blockCount   = (d.block_count as number) ?? scriptBlocks?.length ?? 0
        const durationMin  = d.duration_min as number | undefined

        // Image-specific data
        const validation   = d.validation as { ok: number; total: number; regenerated: number; failed: number; scores: { block_id: string; score: number; ok: boolean; regenerated: boolean; image_url: string }[] } | undefined

        return (
          <div className="border border-amber-600/50 bg-amber-950/40 rounded-lg p-3 space-y-3">

            {/* Header row */}
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-amber-300">{info.icon} {info.title}</div>
                <div className="text-xs text-amber-400/70 mt-0.5">{info.hint}</div>

                {/* Script stats */}
                {liveReviewStage === 'script' && blockCount > 0 && (
                  <div className="text-xs text-amber-300/60 mt-1 tabular-nums">
                    {blockCount} блоків{durationMin ? ` · ~${durationMin} хв` : ''}
                  </div>
                )}

                {/* Image stats */}
                {liveReviewStage === 'images' && validation && (
                  <div className="text-xs mt-1 flex gap-2 tabular-nums">
                    <span className="text-green-400">✓ {validation.ok}/{validation.total} OK</span>
                    {validation.regenerated > 0 && <span className="text-blue-400">↻ {validation.regenerated} regen</span>}
                    {validation.failed > 0      && <span className="text-red-400">✗ {validation.failed} failed</span>}
                  </div>
                )}
              </div>

              <button
                onClick={handleApprove}
                disabled={approving}
                className="shrink-0 px-3 py-1.5 text-sm font-semibold rounded-lg bg-amber-500 hover:bg-amber-400 active:bg-amber-600 text-amber-950 disabled:opacity-50 transition-colors"
              >
                {approving ? 'Approving…' : 'Approve & Continue →'}
              </button>
            </div>

            {/* Script block preview */}
            {liveReviewStage === 'script' && scriptBlocks && scriptBlocks.length > 0 && (
              <div className="max-h-44 overflow-y-auto space-y-0.5 pr-1">
                {scriptBlocks.map((b) => (
                  <div key={b.id} className="flex items-start gap-2 text-xs leading-snug">
                    <span className={`shrink-0 mt-0.5 px-1 py-px rounded text-[10px] font-medium ${BLOCK_TYPE_COLOR[b.type] ?? 'bg-gray-700 text-gray-300'}`}>
                      {b.type}
                    </span>
                    <span className="text-gray-300 line-clamp-2">{b.narration}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Image grid */}
            {liveReviewStage === 'images' && validation?.scores && validation.scores.length > 0 && (
              <div className="grid grid-cols-4 sm:grid-cols-6 gap-1.5">
                {validation.scores.slice(0, 24).map((s) => (
                  <div key={s.block_id} className="relative group">
                    <img
                      src={s.image_url}
                      alt={s.block_id}
                      className="w-full aspect-video object-cover rounded bg-gray-700"
                      loading="lazy"
                    />
                    {/* Score badge */}
                    <div className={`absolute top-0.5 right-0.5 text-[9px] font-bold px-0.5 rounded leading-tight ${
                      s.score >= 8 ? 'bg-green-500 text-white' :
                      s.score >= 7 ? 'bg-yellow-500 text-black' :
                                     'bg-red-500 text-white'
                    }`}>
                      {s.score.toFixed(0)}
                    </div>
                    {/* Regen indicator */}
                    {s.regenerated && (
                      <div className="absolute top-0.5 left-0.5 text-[9px] bg-blue-500 text-white px-0.5 rounded leading-tight">↻</div>
                    )}
                  </div>
                ))}
              </div>
            )}

          </div>
        )
      })()}

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
