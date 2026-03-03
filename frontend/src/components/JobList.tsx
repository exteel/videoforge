import { useEffect, useState, useRef, useCallback } from 'react'
import { type Job, type PipelineRunRequest, type BatchRunRequest, type PromptMeta, api } from '../api'
import { JobCard } from './JobCard'
import { TranscriberPanel } from './TranscriberPanel'

// ── Option configs ─────────────────────────────────────────────────────────────

const QUALITY_OPTS = [
  { value: 'max',      label: 'Max',      desc: 'claude-opus-4-6 — найкраща якість, повільніше' },
  { value: 'high',     label: 'High',     desc: 'claude-sonnet-4-5 — близько до max, 2× дешевше' },
  { value: 'balanced', label: 'Balanced', desc: 'gpt-5.2 — гарна якість, економно' },
  { value: 'bulk',     label: 'Bulk',     desc: 'deepseek-v3.1 — масова генерація, дуже дешево' },
  { value: 'test',     label: 'Test',     desc: 'mistral-small — тільки для тестів пайплайну' },
]

const TEMPLATE_OPTS = [
  { value: 'auto',        label: 'Auto',        desc: 'LLM сам обирає формат під тему' },
  { value: 'documentary', label: 'Documentary', desc: 'Документальний стиль з наративом' },
  { value: 'listicle',    label: 'Listicle',    desc: 'Список фактів / топ-N' },
  { value: 'tutorial',    label: 'Tutorial',    desc: 'Покроковий гайд' },
  { value: 'comparison',  label: 'Comparison',  desc: 'Порівняння двох підходів' },
]

const STEP_OPTS = [
  { value: 1, label: '1 — Script',          desc: 'Генерація сценарію через LLM' },
  { value: 2, label: '2 — Images + Voices', desc: 'Генерація картинок та озвучки' },
  { value: 3, label: '3 — Subtitles',       desc: 'Генерація субтитрів із сценарію' },
  { value: 4, label: '4 — Video',           desc: 'Монтаж відео через FFmpeg' },
  { value: 5, label: '5 — Thumbnail',       desc: 'Генерація та валідація мініатюри' },
  { value: 6, label: '6 — Metadata',        desc: 'Назва, опис, теги для YouTube' },
]

// ── Tooltip ───────────────────────────────────────────────────────────────────

function Tip({ text }: { text: string }) {
  const [show, setShow] = useState(false)
  return (
    <span className="relative inline-block ml-1">
      <button
        type="button"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        className="text-gray-500 hover:text-gray-300 text-xs leading-none"
      >ⓘ</button>
      {show && (
        <span className="absolute z-20 left-5 top-0 w-60 bg-gray-700 text-gray-200 text-xs rounded p-2 shadow-lg border border-gray-600 whitespace-normal">
          {text}
        </span>
      )}
    </span>
  )
}

// ── Select with descriptions ──────────────────────────────────────────────────

interface SelectOption { value: string | number; label: string; desc: string }

function DescSelect({ value, onChange, options, className = '' }: {
  value: string | number
  onChange: (v: string) => void
  options: SelectOption[]
  className?: string
}) {
  return (
    <div className={`relative ${className}`}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 appearance-none pr-6"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} title={o.desc}>
            {o.label} — {o.desc.slice(0, 48)}
          </option>
        ))}
      </select>
      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 text-xs">▾</span>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

const LS_SOURCE_DIR = 'vf_last_source_dir'
const LS_INPUT_DIR  = 'vf_last_input_dir'

type PFormState = PipelineRunRequest & {
  background_music: boolean
  skip_thumbnail: boolean
  image_style: string
  voice_id: string
  duration_min: number
  duration_max: number
  music_volume: number | null
}

export function JobList() {
  const [jobs, setJobs]       = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab]         = useState<'pipeline' | 'batch'>('pipeline')
  const [voices, setVoices]   = useState<{ id: string; name: string }[]>([])
  const [prompts, setPrompts] = useState<PromptMeta[]>([])

  const [pForm, setPForm] = useState<PFormState>({
    source_dir:       localStorage.getItem(LS_SOURCE_DIR) ?? '',
    channel:          'config/channels/history.json',
    quality:          'max',
    template:         'auto',
    draft:            false,
    dry_run:          false,
    from_step:        1,
    to_step:          6,
    background_music: true,
    skip_thumbnail:   false,
    image_style:      '',
    voice_id:         '',
    master_prompt:    null,
    duration_min:     8,
    duration_max:     12,
    music_volume:     null,
  })

  const [bForm, setBForm] = useState<BatchRunRequest>({
    input_dir: localStorage.getItem(LS_INPUT_DIR) ?? '',
    channel:   'config/channels/history.json',
    quality:   'bulk',
    parallel:  1,
    dry_run:   false,
    skip_done: true,
  })

  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError]   = useState('')
  const sourceRef    = useRef<HTMLInputElement>(null)

  // ── Style extractor state ──────────────────────────────────────────────────
  const [styleFile, setStyleFile]       = useState<File | null>(null)
  const [stylePreview, setStylePreview] = useState<string | null>(null)
  const [styleLoading, setStyleLoading] = useState(false)
  const [styleError, setStyleError]     = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const applyStyleImage = useCallback((file: File | null) => {
    setStylePreview((prev) => { if (prev) URL.revokeObjectURL(prev); return null })
    setStyleFile(file)
    setStylePreview(file ? URL.createObjectURL(file) : null)
    setStyleError('')
  }, [])

  // Global paste listener — smart routing:
  //   clipboard image → style extractor (regardless of which field is focused)
  //   clipboard text  → normal browser behavior (focused field gets it, no interception)
  // Guard: if already handled by an element's own onPaste (e.defaultPrevented), skip.
  useEffect(() => {
    function handleGlobalPaste(e: ClipboardEvent) {
      if (e.defaultPrevented) return  // already handled by onPaste on a child element
      const items = Array.from(e.clipboardData?.items ?? [])
      const imageItem = items.find((i) => i.kind === 'file' && i.type.startsWith('image/'))
      if (imageItem) {
        const f = imageItem.getAsFile()
        if (f) {
          e.preventDefault()
          applyStyleImage(f)
        }
      }
      // No image → fall through, browser pastes text into the focused field normally
    }
    document.addEventListener('paste', handleGlobalPaste)
    return () => document.removeEventListener('paste', handleGlobalPaste)
  }, [applyStyleImage])

  async function analyzeStyle() {
    if (!styleFile) return
    setStyleLoading(true)
    setStyleError('')
    try {
      const result = await api.style.analyze(styleFile)
      setPForm((f) => ({ ...f, image_style: result.style }))
    } catch (err) {
      setStyleError(String(err))
    } finally {
      setStyleLoading(false)
    }
  }

  // Load voices + prompts
  useEffect(() => {
    api.voices.list().then(setVoices).catch(() => {})
    api.prompts.list().then(setPrompts).catch(() => {})
  }, [])

  async function loadJobs() {
    try { setJobs(await api.jobs.list(100)) }
    catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => {
    loadJobs()
    const t = setInterval(loadJobs, 3000)
    return () => clearInterval(t)
  }, [])

  function updateSourceDir(val: string) {
    setPForm((f) => ({ ...f, source_dir: val }))
    localStorage.setItem(LS_SOURCE_DIR, val)
  }

  function updateInputDir(val: string) {
    setBForm((f) => ({ ...f, input_dir: val }))
    localStorage.setItem(LS_INPUT_DIR, val)
  }

  async function submitPipeline(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    setSubmitting(true)
    try {
      const payload: Record<string, unknown> = { ...pForm }
      if (!payload.image_style)         delete payload.image_style
      if (!payload.voice_id)            delete payload.voice_id
      if (payload.music_volume == null) delete payload.music_volume
      await api.pipeline.run(payload as unknown as PipelineRunRequest)
      await loadJobs()
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function submitBatch(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    setSubmitting(true)
    try {
      await api.batch.run(bForm)
      await loadJobs()
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const activeJobs = jobs.filter((j) => j.status === 'running' || j.status === 'queued')
  const recentJobs = jobs.filter((j) => j.status !== 'running' && j.status !== 'queued').slice(0, 20)

  function handleTranscriberSelect(dir: string) {
    updateSourceDir(dir)
    setTab('pipeline')
    // Scroll to source dir input
    setTimeout(() => sourceRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
  }

  return (
    <div className="space-y-6">
      {/* Transcriber integration */}
      <TranscriberPanel onSelectDir={handleTranscriberSelect} />

      {/* Launch form */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <div className="flex gap-2 mb-4">
          {(['pipeline', 'batch'] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                tab === t ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              {t === 'pipeline' ? 'Single Video' : 'Batch'}
            </button>
          ))}
        </div>

        {tab === 'pipeline' ? (
          <form onSubmit={submitPipeline} className="space-y-4">

            {/* Source dir */}
            <label className="space-y-1 block">
              <span className="text-xs text-gray-400">
                Source dir *
                <Tip text="Шлях до папки з виходом Transcriber. Підтримує Ctrl+V. Зберігається між сесіями." />
              </span>
              <input
                ref={sourceRef}
                required
                value={pForm.source_dir}
                onChange={(e) => updateSourceDir(e.target.value)}
                onPaste={(e) => {
                  e.preventDefault()
                  updateSourceDir(e.clipboardData.getData('text').trim())
                }}
                placeholder="D:/transscript batch/output/output/Video Title"
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </label>

            {/* Channel + Quality */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Channel config</span>
                <input value={pForm.channel}
                  onChange={(e) => setPForm({ ...pForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">
                  Quality
                  <Tip text="Яку LLM модель використовувати для сценарію. Max = найкраща якість. Test = дешевий режим для налагодження." />
                </span>
                <DescSelect value={pForm.quality ?? 'max'}
                  onChange={(v) => setPForm({ ...pForm, quality: v })}
                  options={QUALITY_OPTS}
                />
              </label>
            </div>

            {/* Template + Step range */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">
                  Template
                  <Tip text="Формат відео. 'Auto' — модель сама обирає. Решта задають чітку структуру сценарію." />
                </span>
                <DescSelect value={pForm.template ?? 'auto'}
                  onChange={(v) => setPForm({ ...pForm, template: v })}
                  options={TEMPLATE_OPTS}
                />
              </label>
              <div className="space-y-1">
                <span className="text-xs text-gray-400">
                  Steps
                  <Tip text="From — з якого кроку стартувати (пропустити вже зроблені). To — на якому зупинитись. Для запуску одного кроку: from=to=N." />
                </span>
                {/* Quick-step presets */}
                <div className="flex flex-wrap gap-1 mb-1">
                  {[
                    { label: 'All', f: 1, t: 6 },
                    { label: '1 Script', f: 1, t: 1 },
                    { label: '2 Images', f: 2, t: 2 },
                    { label: '4 Video',  f: 4, t: 4 },
                    { label: '5 Thumb',  f: 5, t: 5 },
                    { label: '6 Meta',   f: 6, t: 6 },
                  ].map(({ label, f, t }) => {
                    const active = pForm.from_step === f && (pForm.to_step ?? 6) === t
                    return (
                      <button key={label} type="button"
                        onClick={() => setPForm(p => ({ ...p, from_step: f, to_step: t }))}
                        className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                          active
                            ? 'bg-blue-600 text-white'
                            : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                        }`}
                      >{label}</button>
                    )
                  })}
                </div>
                {/* Manual from/to */}
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-500 shrink-0">from</span>
                  <DescSelect value={pForm.from_step ?? 1}
                    onChange={(v) => setPForm(p => ({ ...p, from_step: Number(v), to_step: Math.max(p.to_step ?? 6, Number(v)) }))}
                    options={STEP_OPTS}
                  />
                  <span className="text-xs text-gray-500 shrink-0">to</span>
                  <DescSelect value={pForm.to_step ?? 6}
                    onChange={(v) => setPForm(p => ({ ...p, to_step: Number(v), from_step: Math.min(p.from_step ?? 1, Number(v)) }))}
                    options={STEP_OPTS}
                  />
                </div>
              </div>
            </div>

            {/* Master prompt selector */}
            {prompts.length > 0 && (
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">
                  Master prompt
                  <Tip text="Головний промпт для написання сценарію. Обирай залежно від ніші відео." />
                </span>
                <select
                  value={pForm.master_prompt ?? ''}
                  onChange={(e) => setPForm((f) => ({ ...f, master_prompt: e.target.value || null }))}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  <option value="">(з налаштувань каналу)</option>
                  {prompts.map((p) => (
                    <option key={p.name} value={`prompts/${p.filename}`}>
                      {p.filename} — {Math.round(p.size_bytes / 1024)}KB
                    </option>
                  ))}
                </select>
              </label>
            )}

            {/* Duration range */}
            <div className="space-y-1">
              <span className="text-xs text-gray-400">
                Тривалість відео (хв)
                <Tip text="Цільова тривалість відео. Сценарій генерується під вказаний діапазон. 8-12 хв = стандарт YouTube, 20-30 хв = поглиблений формат." />
              </span>
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500 shrink-0">від</span>
                <input
                  type="number" min="1" max="240" step="1"
                  value={pForm.duration_min}
                  onChange={(e) => {
                    const v = Math.max(1, parseInt(e.target.value) || 1)
                    setPForm((f) => ({ ...f, duration_min: v, duration_max: Math.max(f.duration_max, v) }))
                  }}
                  className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500 shrink-0">до</span>
                <input
                  type="number" min="1" max="240" step="1"
                  value={pForm.duration_max}
                  onChange={(e) => {
                    const v = Math.max(pForm.duration_min, parseInt(e.target.value) || pForm.duration_min)
                    setPForm((f) => ({ ...f, duration_max: v }))
                  }}
                  className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500">
                  ≈ {pForm.duration_min * 140}–{pForm.duration_max * 150} слів
                </span>
              </div>
            </div>

            {/* Voice selector */}
            {voices.length > 0 && (
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">
                  Voice
                  <Tip text="Голос для озвучки. Порожньо = голос з налаштувань каналу." />
                </span>
                <select value={pForm.voice_id}
                  onChange={(e) => setPForm({ ...pForm, voice_id: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  <option value="">(з налаштувань каналу)</option>
                  {voices.map((v) => (
                    <option key={v.id} value={v.id}>{v.name}</option>
                  ))}
                </select>
              </label>
            )}

            {/* Image style */}
            <div className="space-y-1">
              <span className="text-xs text-gray-400">
                Image style
                <Tip text="Стиль візуалу для генерації картинок. Залиш порожнім — береться з налаштувань каналу. Вставте картинку нижче щоб отримати стиль автоматично." />
              </span>

              {/* Style reference image extractor */}
              <div
                className="border border-dashed border-gray-600 rounded p-3 space-y-2 select-none"
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault()
                  const f = e.dataTransfer.files[0]
                  if (f?.type.startsWith('image/')) applyStyleImage(f)
                }}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    if (f) applyStyleImage(f)
                    e.target.value = ''
                  }}
                />
                {stylePreview ? (
                  <div className="flex items-center gap-3">
                    <img
                      src={stylePreview}
                      className="h-16 w-24 object-cover rounded border border-gray-700 shrink-0"
                      alt="ref"
                    />
                    <div className="flex-1 min-w-0 space-y-1">
                      <p className="text-xs text-gray-400 truncate">{styleFile?.name}</p>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={analyzeStyle}
                          disabled={styleLoading}
                          className="px-3 py-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded text-xs transition-colors"
                        >
                          {styleLoading ? 'Аналізую…' : '✦ Аналізувати стиль'}
                        </button>
                        <button
                          type="button"
                          onClick={() => applyStyleImage(null)}
                          className="px-2 py-1 text-gray-500 hover:text-gray-300 rounded text-xs"
                        >✕</button>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs text-gray-500">
                      Ctrl+V або перетягніть картинку референс — отримайте рядок стилю
                    </p>
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      className="shrink-0 px-2 py-1 border border-gray-600 hover:border-gray-400 text-gray-400 hover:text-gray-200 rounded text-xs transition-colors"
                    >
                      Обрати файл
                    </button>
                  </div>
                )}
                {styleError && <p className="text-xs text-red-400">{styleError}</p>}
              </div>

              <input
                value={pForm.image_style}
                onChange={(e) => setPForm({ ...pForm, image_style: e.target.value })}
                onPaste={(e) => {
                  // If clipboard contains an image — load it into style extractor instead of pasting text
                  const items = Array.from(e.clipboardData?.items ?? [])
                  const imageItem = items.find((i) => i.kind === 'file' && i.type.startsWith('image/'))
                  if (imageItem) {
                    e.preventDefault()
                    const f = imageItem.getAsFile()
                    if (f) applyStyleImage(f)
                  }
                  // Text paste falls through to normal input behavior
                }}
                placeholder="cinematic, photorealistic, 8k… або вставте картинку (Ctrl+V)"
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </div>

            {/* Budget */}
            <label className="space-y-1 block">
              <span className="text-xs text-gray-400">Budget USD (optional)</span>
              <input type="number" step="0.01" placeholder="e.g. 5.00"
                value={pForm.budget ?? ''}
                onChange={(e) => setPForm({ ...pForm, budget: e.target.value ? Number(e.target.value) : null })}
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </label>

            {/* Checkboxes */}
            <div className="flex flex-wrap gap-5 text-sm text-gray-300">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.draft}
                  onChange={(e) => setPForm({ ...pForm, draft: e.target.checked })}
                  className="accent-blue-500" />
                <span>Draft (480p)</span>
                <Tip text="Генерує відео 480p без Ken Burns і crossfade. Набагато швидше — щоб перевірити структуру і озвучку перед повним рендером." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.dry_run}
                  onChange={(e) => setPForm({ ...pForm, dry_run: e.target.checked })}
                  className="accent-blue-500" />
                <span>Dry run</span>
                <Tip text="Рахує приблизну вартість БЕЗ реальних API викликів. Жодних кредитів не витрачається — тільки кошторис у консолі." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.background_music}
                  onChange={(e) => setPForm({ ...pForm, background_music: e.target.checked })}
                  className="accent-blue-500" />
                <span>Background music</span>
                <Tip text="Додає royalty-free фонову музику під голос. Гучність регулюється нижче." />
              </label>
              {pForm.background_music && (
                <div className="flex items-center gap-2 ml-1">
                  <span className="text-xs text-gray-400 shrink-0">Гучність БГМ:</span>
                  <input
                    type="number" min={-60} max={-10} step={1}
                    value={pForm.music_volume ?? -28}
                    onChange={(e) =>
                      setPForm((f) => ({
                        ...f,
                        music_volume: e.target.value === '' ? null : Number(e.target.value),
                      }))
                    }
                    className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-sm text-white text-center focus:outline-none focus:border-blue-500"
                  />
                  <span className="text-xs text-gray-500">dB</span>
                  <Tip text="-28 = тихо (рекомендовано), -20 = стандарт. Менше число = тихіше." />
                </div>
              )}
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.skip_thumbnail}
                  onChange={(e) => setPForm({ ...pForm, skip_thumbnail: e.target.checked })}
                  className="accent-blue-500" />
                <span>Skip thumbnail</span>
                <Tip text="Пропустити генерацію thumbnail (Step 5). Корисно коли thumbnail вже є або потрібно швидко отримати відео." />
              </label>
            </div>

            {formError && (
              <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>
            )}
            <button type="submit" disabled={submitting}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded transition-colors"
            >
              {submitting ? 'Starting…' : 'Run Pipeline'}
            </button>
          </form>

        ) : (
          <form onSubmit={submitBatch} className="space-y-4">
            <label className="space-y-1 block">
              <span className="text-xs text-gray-400">
                Input dir *
                <Tip text="Папка що містить підпапки з Transcriber виходом. Кожна підпапка = одне відео." />
              </span>
              <input required value={bForm.input_dir}
                onChange={(e) => updateInputDir(e.target.value)}
                onPaste={(e) => { e.preventDefault(); updateInputDir(e.clipboardData.getData('text').trim()) }}
                placeholder="D:/transscript batch/output/output"
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </label>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Channel config</span>
                <input value={bForm.channel}
                  onChange={(e) => setBForm({ ...bForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Quality</span>
                <DescSelect value={bForm.quality ?? 'bulk'}
                  onChange={(v) => setBForm({ ...bForm, quality: v })}
                  options={QUALITY_OPTS}
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">From step</span>
                <DescSelect value={bForm.from_step ?? 1}
                  onChange={(v) => setBForm({ ...bForm, from_step: Number(v) })}
                  options={STEP_OPTS}
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Parallel workers</span>
                <input type="number" min={1} max={8} value={bForm.parallel}
                  onChange={(e) => setBForm({ ...bForm, parallel: Number(e.target.value) })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
            </div>
            <div className="flex flex-wrap gap-5 text-sm text-gray-300">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={!!bForm.skip_done}
                  onChange={(e) => setBForm({ ...bForm, skip_done: e.target.checked })}
                  className="accent-blue-500" />
                <span>Skip done</span>
                <Tip text="Пропускає відео де вже є final.mp4." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={!!bForm.dry_run}
                  onChange={(e) => setBForm({ ...bForm, dry_run: e.target.checked })}
                  className="accent-blue-500" />
                <span>Dry run</span>
                <Tip text="Рахує вартість без API викликів." />
              </label>
            </div>
            {formError && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>}
            <button type="submit" disabled={submitting}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
            >
              {submitting ? 'Starting…' : 'Run Batch'}
            </button>
          </form>
        )}
      </div>

      {/* Active jobs */}
      {activeJobs.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            Active ({activeJobs.length})
          </h2>
          {activeJobs.map((j) => <JobCard key={j.job_id} job={j} onRefresh={loadJobs} />)}
        </div>
      )}

      {/* Recent jobs */}
      {recentJobs.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">Recent</h2>
          {recentJobs.map((j) => <JobCard key={j.job_id} job={j} onRefresh={loadJobs} />)}
        </div>
      )}

      {!loading && jobs.length === 0 && (
        <p className="text-gray-500 text-sm text-center py-8">No jobs yet. Run a pipeline above.</p>
      )}
    </div>
  )
}
