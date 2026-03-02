/**
 * TranscriberPanel — YouTube URL → Download → Transcribe → Pipeline.
 *
 * Основний flow:
 *   1. Вставити YouTube URL (або кілька)
 *   2. Вибрати опції (мова, якість, auto-pipeline)
 *   3. Натиснути Start — VideoForge сам качає, транскрибує, запускає пайплайн
 *
 * Альтернативний flow (зовнішній Transcriber):
 *   - Кнопка "Open Transcriber" → відкриває окреме вікно Transcriber GUI
 *   - Scan → знаходить вже готові виходи → клік ▶ Pipeline
 */

import { useRef, useState } from 'react'
import {
  api,
  type TranscribeJob,
  type TranscriberOutput,
} from '../api'

// ── Constants ─────────────────────────────────────────────────────────────────

const QUALITY_OPTS = [
  { value: 'max',      label: 'Max',      desc: 'claude-opus-4-6' },
  { value: 'high',     label: 'High',     desc: 'claude-sonnet-4-5' },
  { value: 'balanced', label: 'Balanced', desc: 'gpt-5.2' },
  { value: 'bulk',     label: 'Bulk',     desc: 'deepseek-v3.1' },
  { value: 'test',     label: 'Test',     desc: 'mistral-small' },
]

const STATUS_COLOR: Record<string, string> = {
  queued:  'bg-yellow-900 text-yellow-300',
  running: 'bg-blue-900 text-blue-300',
  done:    'bg-green-900 text-green-300',
  failed:  'bg-red-900 text-red-300',
}

// ── Job card ──────────────────────────────────────────────────────────────────

import { useEffect } from 'react'

function JobCard({
  initialJob,
  onDone,
}: {
  initialJob: TranscribeJob
  onDone: (dir: string) => void
}) {
  const [job, setJob] = useState(initialJob)
  const doneRef = useRef(false)

  useEffect(() => {
    if (job.status === 'done' || job.status === 'failed') return
    const id = setInterval(async () => {
      try {
        const updated = await api.transcribe.get(job.job_id)
        setJob(updated)
        if ((updated.status === 'done' || updated.status === 'failed') && !doneRef.current) {
          doneRef.current = true
          if (updated.status === 'done' && updated.out_dir) {
            onDone(updated.out_dir)
          }
        }
      } catch { /* ignore */ }
    }, 1500)
    return () => clearInterval(id)
  }, [job.job_id, job.status])

  const isActive = job.status === 'running' || job.status === 'queued'

  return (
    <div className="bg-gray-900 rounded border border-gray-700 p-3 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${STATUS_COLOR[job.status] ?? 'bg-gray-700 text-gray-300'}`}>
          {job.status}
        </span>
        <span className="text-xs text-gray-400 truncate max-w-sm">{job.url}</span>
      </div>

      {/* Live logs */}
      {job.logs.length > 0 && (
        <div className="font-mono text-xs space-y-0.5">
          {job.logs.slice(-4).map((l, i) => (
            <div
              key={i}
              className={
                isActive && i === Math.min(job.logs.length, 4) - 1
                  ? 'text-blue-300'
                  : 'text-gray-400'
              }
            >
              {l}
            </div>
          ))}
        </div>
      )}

      {job.status === 'done' && job.out_dir && (
        <div className="text-xs text-green-400 truncate">✓ {job.out_dir}</div>
      )}

      {job.error && (
        <div className="text-xs text-red-300 bg-red-950 rounded p-1.5">✗ {job.error}</div>
      )}

      {isActive && (
        <div className="w-full bg-gray-700 rounded-full h-1">
          <div className="bg-blue-500 h-1 rounded-full animate-pulse w-full" />
        </div>
      )}
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────

interface Props {
  onSelectDir: (dir: string) => void
}

export function TranscriberPanel({ onSelectDir }: Props) {
  // Form
  const [urls, setUrls]             = useState('')
  const [language, setLanguage]     = useState('')
  const [quality, setQuality]       = useState('max')
  const [channel, setChannel]       = useState('config/channels/history.json')
  const [autoPipeline, setAutoPipeline] = useState(false)
  const [skipThumbnail, setSkipThumbnail] = useState(false)
  const [durationMin, setDurationMin] = useState(8)
  const [durationMax, setDurationMax] = useState(12)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError]   = useState('')

  // Jobs list
  const [jobs, setJobs] = useState<TranscribeJob[]>([])

  // External transcriber section
  const [outputs, setOutputs]     = useState<TranscriberOutput[]>([])
  const [scanning, setScanning]   = useState(false)
  const [showExternal, setShowExternal] = useState(false)

  // Collapsed
  const [open, setOpen] = useState(true)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    const lines = urls.split('\n').map(s => s.trim()).filter(Boolean)
    if (!lines.length) { setFormError('Вставте хоча б одне посилання'); return }

    setSubmitting(true)
    const newJobs: TranscribeJob[] = []
    try {
      for (const url of lines) {
        const res = await api.transcribe.start({
          url,
          language:       language || undefined,
          auto_pipeline:  autoPipeline,
          channel,
          quality,
          duration_min:   durationMin,
          duration_max:   durationMax,
          skip_thumbnail: skipThumbnail,
        })
        newJobs.push({
          job_id: res.job_id,
          url,
          status: res.status,
          logs: [],
          error: '',
          out_dir: '',
        })
      }
      setJobs(prev => [...newJobs, ...prev])
      setUrls('')
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleScan() {
    setScanning(true)
    try { setOutputs(await api.transcriber.outputs()) }
    finally { setScanning(false) }
  }

  function handleJobDone(dir: string) {
    // If auto_pipeline is off — fill Jobs form source_dir
    if (!autoPipeline) onSelectDir(dir)
  }

  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
      {/* Toggle header */}
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-700/50 transition-colors"
      >
        <span className="text-sm font-semibold text-white">
          📥 Download &amp; Transcribe
        </span>
        <span className="text-gray-500 text-xs">{open ? '▲ hide' : '▼ show'}</span>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-4 border-t border-gray-700 pt-4">

          {/* ── URL form ─────────────────────────────────────────────────── */}
          <form onSubmit={handleSubmit} className="space-y-3">
            <label className="block space-y-1">
              <span className="text-xs text-gray-400">
                YouTube URL(s) — по одному на рядок, або Ctrl+V щоб вставити
              </span>
              <textarea
                value={urls}
                onChange={e => setUrls(e.target.value)}
                onPaste={e => {
                  e.preventDefault()
                  const pasted = e.clipboardData.getData('text').trim()
                  setUrls(prev => prev ? `${prev}\n${pasted}` : pasted)
                }}
                placeholder={"https://www.youtube.com/watch?v=dQw4w9WgXcQ\nhttps://youtu.be/..."}
                rows={3}
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 resize-none font-mono"
              />
            </label>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              {/* Language */}
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Мова (auto якщо порожньо)</span>
                <input
                  value={language}
                  onChange={e => setLanguage(e.target.value)}
                  placeholder="uk / en / de…"
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>

              {/* Quality */}
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Якість LLM</span>
                <select
                  value={quality}
                  onChange={e => setQuality(e.target.value)}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {QUALITY_OPTS.map(o => (
                    <option key={o.value} value={o.value}>
                      {o.label} — {o.desc}
                    </option>
                  ))}
                </select>
              </label>

              {/* Channel */}
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Channel config</span>
                <input
                  value={channel}
                  onChange={e => setChannel(e.target.value)}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>

              {/* Auto-pipeline toggle */}
              <div className="flex flex-col justify-end gap-1 pb-0.5">
                <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoPipeline}
                    onChange={e => setAutoPipeline(e.target.checked)}
                    className="accent-blue-500"
                  />
                  Auto-pipeline
                </label>
                <span className="text-xs text-gray-500 leading-tight">
                  {autoPipeline
                    ? 'Запускає пайплайн одразу після транскрипції'
                    : 'Підставляє шлях у форму Jobs вручну'}
                </span>
              </div>
            </div>

            {/* Skip thumbnail — visible only when auto-pipeline is ON */}
            {autoPipeline && (
              <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={skipThumbnail}
                  onChange={e => setSkipThumbnail(e.target.checked)}
                  className="accent-blue-500"
                />
                <span>Skip thumbnail</span>
                <span className="text-xs text-gray-500">Пропустити генерацію thumbnail (Step 5)</span>
              </label>
            )}

            {/* Duration range — visible only when auto-pipeline is ON */}
            {autoPipeline && (
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400 shrink-0">Тривалість (хв):</span>
                <span className="text-xs text-gray-500 shrink-0">від</span>
                <input
                  type="number" min="1" max="240" step="1"
                  value={durationMin}
                  onChange={e => {
                    const v = Math.max(1, parseInt(e.target.value) || 1)
                    setDurationMin(v)
                    setDurationMax(prev => Math.max(prev, v))
                  }}
                  className="w-16 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500 shrink-0">до</span>
                <input
                  type="number" min="1" max="240" step="1"
                  value={durationMax}
                  onChange={e => {
                    const v = Math.max(durationMin, parseInt(e.target.value) || durationMin)
                    setDurationMax(v)
                  }}
                  className="w-16 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500">
                  ≈ {durationMin * 140}–{durationMax * 150} слів
                </span>
              </div>
            )}

            {formError && (
              <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>
            )}

            <button
              type="submit"
              disabled={submitting || !urls.trim()}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded transition-colors"
            >
              {submitting ? '⏳ Запускаємо…' : '⬇ Download & Transcribe'}
            </button>
          </form>

          {/* ── Jobs ─────────────────────────────────────────────────────── */}
          {jobs.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-medium text-gray-400 uppercase tracking-wide">
                Transcription jobs ({jobs.length})
              </div>
              {jobs.map(j => (
                <JobCard key={j.job_id} initialJob={j} onDone={handleJobDone} />
              ))}
            </div>
          )}

          {/* ── External Transcriber (collapsible) ───────────────────────── */}
          <div className="border-t border-gray-700 pt-3">
            <button
              onClick={() => {
                setShowExternal(v => !v)
                if (!showExternal) handleScan()
              }}
              className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              {showExternal ? '▲' : '▼'} Є готові виходи від Transcriber GUI?
            </button>

            {showExternal && (
              <div className="mt-3 space-y-2">
                <div className="flex gap-2">
                  <button
                    onClick={() => api.transcriber.launch().catch(() => {})}
                    className="text-xs px-3 py-1.5 rounded bg-indigo-700 hover:bg-indigo-600 text-white"
                  >
                    🚀 Open Transcriber
                  </button>
                  <button
                    onClick={handleScan}
                    disabled={scanning}
                    className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-gray-300"
                  >
                    {scanning ? '⏳' : '↻'} Scan outputs
                  </button>
                </div>

                {outputs.map(o => (
                  <div
                    key={o.dir}
                    className="flex items-center justify-between gap-2 bg-gray-900 rounded p-2 border border-gray-700 hover:border-blue-600 transition-colors"
                  >
                    <div className="min-w-0">
                      <div className="text-xs text-white truncate">{o.title || o.name}</div>
                      <div className="text-xs text-gray-500 space-x-1">
                        {o.language && <span>{o.language}</span>}
                        {o.has_srt && <span>· SRT</span>}
                        {o.has_thumbnail && <span>· 🖼</span>}
                      </div>
                    </div>
                    <button
                      onClick={() => onSelectDir(o.dir)}
                      className="shrink-0 text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white"
                    >
                      ▶ Pipeline
                    </button>
                  </div>
                ))}

                {outputs.length === 0 && !scanning && (
                  <div className="text-xs text-gray-500">
                    Виходів не знайдено. Запусти Transcriber і натисни Scan.
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
