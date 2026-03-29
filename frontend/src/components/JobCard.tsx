import { useState, useEffect, useRef } from 'react'
import { type Job, type DriveUploadJob, api } from '../api'
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
  const [regenerating, setRegenerating] = useState(false)
  const [regenScript, setRegenScript] = useState(false)
  const [liveSec, setLiveSec] = useState<number | null>(null)

  // ── Drive upload ───────────────────────────────────────────────────────────
  const [driveJob,     setDriveJob]     = useState<DriveUploadJob | null>(null)
  const [driveLoading, setDriveLoading] = useState(false)
  const driveTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  async function uploadToDrive() {
    if (!job.project_dir) return
    setDriveLoading(true)
    try {
      const { upload_id } = await api.drive.upload({
        project_dir: job.project_dir,
        channel: job.channel ? `config/channels/${job.channel}.json` : undefined,
      })
      // Poll until done
      driveTimerRef.current = setInterval(async () => {
        try {
          const status = await api.drive.uploadStatus(upload_id)
          setDriveJob(status)
          if (status.status !== 'running') {
            clearInterval(driveTimerRef.current!)
            driveTimerRef.current = null
            setDriveLoading(false)
          }
        } catch { /* ignore poll error */ }
      }, 2000)
    } catch (e) {
      setDriveJob({ upload_id: '', status: 'failed', project_dir: job.project_dir,
        channel_name: '', folder_url: null, uploaded_files: [], error: String(e) })
      setDriveLoading(false)
    }
  }

  useEffect(() => () => { if (driveTimerRef.current) clearInterval(driveTimerRef.current) }, [])

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

  async function handleRegenImages() {
    setRegenerating(true)
    try {
      await api.jobs.regenImages(job.job_id)
      // WS will push updated review_data automatically
    } catch (err) {
      console.error('Regen failed:', err)
    } finally {
      setRegenerating(false)
    }
  }

  async function handleRegenScript() {
    setRegenScript(true)
    try {
      await api.jobs.regenScript(job.job_id)
      // WS will push review_required with new review_data
    } catch (err) {
      console.error('Script regen failed:', err)
    } finally {
      setRegenScript(false)
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

        // ── Script data ───────────────────────────────────────────────────
        type BlockSummary = { id: string; type: string; title: string; word_count: number; image_count: number; narration: string; est_duration_sec?: number }
        const scriptBlocks  = d.blocks as BlockSummary[] | undefined
        const blockCount    = (d.block_count as number) ?? scriptBlocks?.length ?? 0
        const wordCount     = d.word_count as number | undefined
        const durMin        = d.duration_min as number | undefined
        const durMax        = d.duration_max as number | undefined
        const typeCounts    = (d.type_counts ?? {}) as Record<string, number>
        const imgCount      = d.image_prompt_count as number | undefined
        const hasHook       = d.has_hook as boolean | undefined
        const scriptTitle   = d.title as string | undefined

        // ── Image data ────────────────────────────────────────────────────
        type ImgScore = { block_id: string; label: string; score: number; ok: boolean; regenerated: boolean; attempts: number; reason: string; improved_prompt: string; skipped: boolean; skip_reason: string; image_url: string }
        const _rawVal = d.validation as { ok?: number; total?: number; regenerated?: number; failed?: number; skipped?: number; scores?: ImgScore[] } | undefined
        // Only treat as valid if total is a real number (not empty {})
        const validation  = (_rawVal && typeof _rawVal.total === 'number') ? _rawVal as { ok: number; total: number; regenerated: number; failed: number; skipped: number; scores: ImgScore[] } : undefined
        const imgOk       = validation?.ok ?? 0
        const imgTotal    = validation?.total ?? 0
        const imgSkipped  = validation ? (validation.skipped ?? 0) : 0
        const imgOkPct    = imgTotal > 0 ? Math.round((imgOk / imgTotal) * 100) : 0
        const imgSufficient = imgTotal > 0 && imgOk >= Math.ceil(imgTotal * 0.8)
        const failedScores = validation?.scores?.filter((s) => !s.ok && !s.skipped) ?? []
        const skippedScores = validation?.scores?.filter((s) => s.skipped) ?? []

        return (
          <div className="border border-amber-600/50 bg-amber-950/40 rounded-lg p-3 space-y-3">

            {/* ── Header row ──────────────────────────────────────────────── */}
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-amber-300">{info.icon} {info.title}</div>
                {scriptTitle && <div className="text-xs text-white mt-0.5 truncate">«{scriptTitle}»</div>}
                {!scriptTitle && <div className="text-xs text-amber-400/70 mt-0.5">{info.hint}</div>}
              </div>
              <div className="flex gap-2 shrink-0 flex-wrap">
                {liveReviewStage === 'script' && (
                  <button
                    onClick={handleRegenScript}
                    disabled={regenScript || approving}
                    className="px-3 py-1.5 text-sm font-semibold rounded-lg bg-blue-700 hover:bg-blue-600 active:bg-blue-800 text-white disabled:opacity-50 transition-colors"
                  >
                    {regenScript ? '↻ Генерація…' : '↻ Перегенерувати'}
                  </button>
                )}
                {liveReviewStage === 'images' && validation && validation.failed > 0 && (
                  <button
                    onClick={handleRegenImages}
                    disabled={regenerating || approving}
                    className="px-3 py-1.5 text-sm font-semibold rounded-lg bg-blue-600 hover:bg-blue-500 active:bg-blue-700 text-white disabled:opacity-50 transition-colors"
                  >
                    {regenerating ? '↻ Генерація…' : `↻ Перегенерувати (${validation.failed})`}
                  </button>
                )}
                <button
                  onClick={handleApprove}
                  disabled={approving || regenerating}
                  className="px-3 py-1.5 text-sm font-semibold rounded-lg bg-amber-500 hover:bg-amber-400 active:bg-amber-600 text-amber-950 disabled:opacity-50 transition-colors"
                >
                  {approving ? 'Approving…' : 'Approve & Continue →'}
                </button>
              </div>
            </div>

            {/* ══════════════════════════════════════════════════════════════
                SCRIPT REVIEW
                ══════════════════════════════════════════════════════════ */}
            {liveReviewStage === 'script' && (
              <>
                {/* ── Metrics row ───────────────────────────────────────── */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {/* Duration */}
                  <div className="bg-gray-900 rounded p-2 text-center">
                    <div className="text-base font-bold text-amber-300 tabular-nums">
                      {durMin != null && durMax != null ? `${durMin}–${durMax}` : durMin ?? '?'} хв
                    </div>
                    <div className="text-[10px] text-gray-500 mt-0.5">тривалість</div>
                  </div>
                  {/* Words */}
                  <div className="bg-gray-900 rounded p-2 text-center">
                    <div className="text-base font-bold text-white tabular-nums">{wordCount?.toLocaleString() ?? '?'}</div>
                    <div className="text-[10px] text-gray-500 mt-0.5">слів</div>
                  </div>
                  {/* Image prompts */}
                  <div className="bg-gray-900 rounded p-2 text-center">
                    <div className="text-base font-bold text-blue-300 tabular-nums">{imgCount ?? '?'}</div>
                    <div className="text-[10px] text-gray-500 mt-0.5">картинок</div>
                  </div>
                  {/* Hook */}
                  <div className={`rounded p-2 text-center ${hasHook ? 'bg-green-950' : 'bg-red-950'}`}>
                    <div className={`text-base font-bold ${hasHook ? 'text-green-400' : 'text-red-400'}`}>
                      {hasHook ? '✓ Є' : '✗ Нема'}
                    </div>
                    <div className="text-[10px] text-gray-500 mt-0.5">hook</div>
                  </div>
                </div>

                {/* ── Structure breakdown ───────────────────────────────── */}
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                  <span className="text-gray-400">Структура:</span>
                  {Object.entries(typeCounts).map(([type, cnt]) => (
                    <span key={type} className="flex items-center gap-1">
                      <span className={`px-1 py-px rounded text-[10px] font-medium ${BLOCK_TYPE_COLOR[type] ?? 'bg-gray-700 text-gray-300'}`}>{type}</span>
                      <span className="text-gray-300 tabular-nums">×{cnt}</span>
                    </span>
                  ))}
                  <span className="text-gray-500 ml-auto tabular-nums">{blockCount} блоків</span>
                </div>

                {/* ── Block list (compact, scrollable) ─────────────────── */}
                {scriptBlocks && scriptBlocks.length > 0 && (
                  <div className="max-h-52 overflow-y-auto space-y-0.5 pr-1">
                    {scriptBlocks.map((b) => (
                      <details key={b.id} className="group">
                        <summary className="flex items-center gap-2 text-xs cursor-pointer select-none hover:bg-gray-700/40 rounded px-1 py-0.5 list-none">
                          {/* Type badge */}
                          <span className={`shrink-0 px-1 py-px rounded text-[10px] font-medium w-14 text-center ${BLOCK_TYPE_COLOR[b.type] ?? 'bg-gray-700 text-gray-300'}`}>
                            {b.type}
                          </span>
                          {/* Title or narration preview */}
                          <span className="flex-1 text-gray-300 truncate">{b.title || b.narration.slice(0, 80)}</span>
                          {/* Stats */}
                          <span className="shrink-0 tabular-nums text-gray-500 text-[10px] flex gap-1.5">
                            <span title="слів">{b.word_count}w</span>
                            {b.est_duration_sec != null && <span className="text-gray-500">~{Math.round(b.est_duration_sec)}s</span>}
                            {b.image_count > 0 && <span title="зображень" className="text-blue-400">🖼{b.image_count}</span>}
                          </span>
                        </summary>
                        {/* Expanded narration */}
                        <div className="text-[11px] text-gray-400 pl-16 pr-2 pb-1 pt-0.5 leading-relaxed">
                          {b.narration || <span className="italic text-gray-600">пусто</span>}
                        </div>
                      </details>
                    ))}
                  </div>
                )}
              </>
            )}

            {/* ══════════════════════════════════════════════════════════════
                IMAGE REVIEW
                ══════════════════════════════════════════════════════════ */}
            {liveReviewStage === 'images' && !validation && (
              <div className="text-xs text-amber-400/70 italic py-1">
                ⚠ Дані валідації відсутні — 02b_image_validator упав або не запустився (дивись логи)
              </div>
            )}
            {liveReviewStage === 'images' && validation && (
              <>
                {/* ── Summary row ───────────────────────────────────────── */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {/* OK / total */}
                  <div className={`rounded p-2 text-center ${imgSufficient ? 'bg-green-950' : 'bg-red-950'}`}>
                    <div className={`text-base font-bold tabular-nums ${imgSufficient ? 'text-green-400' : 'text-red-400'}`}>
                      {imgOk}/{imgTotal}
                    </div>
                    <div className="text-[10px] text-gray-500 mt-0.5">{imgSufficient ? '✓ достатньо' : '✗ мало OK'}</div>
                  </div>
                  {/* OK % */}
                  <div className="bg-gray-900 rounded p-2 text-center">
                    <div className="text-base font-bold tabular-nums text-white">{imgOkPct}%</div>
                    <div className="text-[10px] text-gray-500 mt-0.5">якість (поріг: 7/10)</div>
                  </div>
                  {/* Regenerated */}
                  <div className="bg-gray-900 rounded p-2 text-center">
                    <div className={`text-base font-bold tabular-nums ${validation.regenerated > 0 ? 'text-blue-400' : 'text-gray-500'}`}>
                      {validation.regenerated}
                    </div>
                    <div className="text-[10px] text-gray-500 mt-0.5">↻ перегенеровано</div>
                  </div>
                  {/* Failed + skipped */}
                  <div className={`rounded p-2 text-center ${(validation.failed + imgSkipped) > 0 ? 'bg-red-950' : 'bg-gray-900'}`}>
                    <div className={`text-base font-bold tabular-nums ${(validation.failed + imgSkipped) > 0 ? 'text-red-400' : 'text-gray-500'}`}>
                      {validation.failed}{imgSkipped > 0 ? `+${imgSkipped}` : ''}
                    </div>
                    <div className="text-[10px] text-gray-500 mt-0.5">✗ провал{imgSkipped > 0 ? '+пропуск' : ''}</div>
                  </div>
                </div>

                {/* ── Image grid with inline score labels ───────────────── */}
                {validation.scores && validation.scores.length > 0 && (
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-1.5">
                    {validation.scores.slice(0, 36).map((s) => (
                      <div key={s.block_id + s.label} className="relative group">
                        {s.skipped ? (
                          <div className="w-full aspect-video rounded bg-gray-800 border border-gray-700 flex flex-col items-center justify-center gap-0.5 p-1">
                            <span className="text-[9px] text-gray-500 font-mono text-center leading-tight">{s.label || s.block_id}</span>
                            <span className="text-[8px] text-orange-400">пропущено</span>
                          </div>
                        ) : (
                          <img
                            src={s.image_url}
                            alt={s.block_id}
                            className={`w-full aspect-video object-cover rounded bg-gray-700 ${
                              !s.ok ? 'ring-2 ring-red-500' : s.regenerated ? 'ring-1 ring-blue-500' : ''
                            }`}
                            loading="lazy"
                          />
                        )}
                        {/* Score badge */}
                        {!s.skipped && (
                          <div className={`absolute top-0.5 right-0.5 text-[9px] font-bold px-1 py-px rounded leading-tight ${
                            s.score >= 8 ? 'bg-green-600 text-white' :
                            s.score >= 7 ? 'bg-yellow-500 text-black' :
                                           'bg-red-600 text-white'
                          }`}>
                            {Number(s.score).toFixed(0)}
                          </div>
                        )}
                        {/* Regen indicator */}
                        {s.regenerated && (
                          <div className="absolute top-0.5 left-0.5 text-[9px] bg-blue-600 text-white px-0.5 rounded leading-tight">↻</div>
                        )}
                        {/* Attempts badge (show if > 1) */}
                        {(s.attempts ?? 1) > 1 && (
                          <div className="absolute bottom-0.5 left-0.5 text-[9px] bg-gray-800/80 text-gray-300 px-0.5 rounded leading-tight">
                            ×{s.attempts}
                          </div>
                        )}
                        {/* Reason tooltip on hover */}
                        {(s.reason || s.skip_reason) && (
                          <div className="absolute inset-x-0 bottom-full mb-1 hidden group-hover:flex z-10 justify-center px-1">
                            <div className="bg-gray-900 border border-gray-600 text-[10px] text-gray-200 rounded px-2 py-1 shadow-lg max-w-[160px] text-center leading-snug">
                              {s.reason || s.skip_reason}
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* ── Failed images detail list ──────────────────────────── */}
                {failedScores.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-red-400 font-semibold uppercase tracking-wide">
                      ✗ Не пройшли валідацію ({failedScores.length})
                    </div>
                    <div className="max-h-40 overflow-y-auto space-y-0.5 pr-1">
                      {failedScores.map((s) => (
                        <details key={s.block_id + s.label} className="group">
                          <summary className="flex items-center gap-2 text-xs cursor-pointer select-none hover:bg-gray-700/40 rounded px-1 py-0.5 list-none">
                            <span className="shrink-0 bg-red-900 text-red-300 text-[10px] font-bold px-1.5 py-px rounded tabular-nums">
                              {Number(s.score).toFixed(0)}/10
                            </span>
                            <span className="font-mono text-gray-400 text-[10px] shrink-0">{s.label || s.block_id}</span>
                            <span className="flex-1 text-gray-400 truncate text-[10px]">{s.reason}</span>
                            {(s.attempts ?? 1) > 1 && (
                              <span className="shrink-0 text-[9px] text-blue-400">×{s.attempts} спроб</span>
                            )}
                          </summary>
                          {s.improved_prompt && (
                            <div className="text-[10px] text-blue-300 pl-8 pr-2 pb-1 pt-0.5 leading-relaxed italic">
                              💡 {s.improved_prompt}
                            </div>
                          )}
                        </details>
                      ))}
                    </div>
                  </div>
                )}

                {/* ── Skipped images list ────────────────────────────────── */}
                {skippedScores.length > 0 && (
                  <div className="space-y-0.5">
                    <div className="text-[10px] text-orange-400 font-semibold uppercase tracking-wide">
                      ⚠ Пропущено ({skippedScores.length})
                    </div>
                    {skippedScores.map((s) => (
                      <div key={s.block_id + s.label} className="flex items-center gap-2 text-[10px] text-gray-500 px-1">
                        <span className="font-mono shrink-0">{s.label || s.block_id}</span>
                        <span className="truncate">{s.skip_reason || 'невідома причина'}</span>
                      </div>
                    ))}
                  </div>
                )}
              </>
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

      {/* Drive upload */}
      {job.project_dir && (
        <div className="flex gap-2 flex-wrap">
          {/* Drive upload — only for completed jobs with a project dir */}
          {job.status === 'done' && job.project_dir && !driveJob && (
            <button
              onClick={uploadToDrive}
              disabled={driveLoading}
              className="text-xs px-2 py-1 rounded bg-green-900/60 hover:bg-green-800/70 text-green-300 disabled:opacity-50 transition-colors"
            >
              {driveLoading ? '⏳ Uploading…' : '☁ Drive'}
            </button>
          )}
          {driveJob && driveJob.status === 'running' && (
            <span className="text-xs text-blue-300">☁ Uploading to Drive…</span>
          )}
          {driveJob && driveJob.status === 'done' && driveJob.folder_url && (
            <div className="flex items-center gap-1">
              <a
                href={driveJob.folder_url}
                target="_blank"
                rel="noreferrer"
                className="text-xs px-2 py-1 rounded bg-green-900/60 text-green-300 hover:bg-green-800/70 transition-colors"
              >
                ☁ Drive ✓ ({driveJob.uploaded_files.length} files) ↗
              </a>
              <button
                onClick={() => setDriveJob(null)}
                className="text-xs px-1.5 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-400 transition-colors"
                title="Завантажити знову"
              >↺</button>
            </div>
          )}
          {driveJob && driveJob.status === 'failed' && (
            <div className="flex items-center gap-1">
              <span className="text-xs text-red-300" title={driveJob.error ?? ''}>
                ☁ Drive failed
              </span>
              <button
                onClick={() => setDriveJob(null)}
                className="text-xs px-1.5 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-400 transition-colors"
                title="Спробувати знову"
              >↺</button>
            </div>
          )}
        </div>
      )}

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
