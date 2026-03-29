import { useEffect, useState, useRef, useCallback } from 'react'
import { type Job, type PipelineRunRequest, type BatchRunRequest, type MultiBatchRequest, type MultiTopicItem, type PromptMeta, type MusicTrack, type QuickBatchRequest, type Preset, api } from '../api'
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

const LS_SOURCE_DIR    = 'vf_last_source_dir'
const LS_INPUT_DIR     = 'vf_last_input_dir'
const LS_MULTI_ITEMS   = 'vf_multi_items'
const LS_MULTI_SETTINGS = 'vf_multi_settings'
const LS_PRESET_ID     = 'vf_last_preset_id'

function lsGet<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem(key); return v ? JSON.parse(v) as T : fallback }
  catch { return fallback }
}
function lsSet(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* quota */ }
}

type PFormState = PipelineRunRequest & {
  channel: string           // override optional → required
  quality: string           // override optional → required
  template: string          // override optional → required
  draft: boolean            // override optional → required
  dry_run: boolean          // override optional → required
  background_music: boolean // override optional → required
  skip_thumbnail: boolean   // override optional → required
  burn_subtitles: boolean   // override optional → required
  no_ken_burns: boolean     // override optional → required
  auto_approve: boolean     // override optional → required
  force: boolean            // override optional → required
  image_style: string       // override optional → required
  voice_id: string          // override optional → required
  duration_min: number      // override optional → required
  duration_max: number      // override optional → required
  master_prompt: string | null  // override optional → string | null
  music_volume: number | null
  custom_topic: string      // topic override for script generation
  image_backend: string     // "" (auto from channel config) | "wavespeed" | "voiceimage" | "betatest" | "voidai"
  vision_model: string      // "gpt-4.1" | "gpt-4.1-mini"
}

export function JobList() {
  const [jobs, setJobs]       = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab]         = useState<'pipeline' | 'batch' | 'multi'>('pipeline')
  const [voices, setVoices]   = useState<{ id: string; name: string }[]>([])
  const [prompts, setPrompts] = useState<PromptMeta[]>([])
  const [musicTracks, setMusicTracks] = useState<MusicTrack[]>([])
  const [channels, setChannels] = useState<{ name: string; channel_name: string }[]>([])
  const [projectFolders, setProjectFolders] = useState<{ name: string; has_config: boolean; video_count: number }[]>([])
  const [refreshingFolders, setRefreshingFolders] = useState(false)
  const [presets, setPresets]           = useState<Preset[]>([])
  const [selectedPresetId, setSelectedPresetId] = useState<string>(
    localStorage.getItem(LS_PRESET_ID) ?? ''
  )
  const [driveStatus, setDriveStatus]   = useState<{ authenticated: boolean; root_folder_id: string } | null>(null)
  const [driveAuthing, setDriveAuthing] = useState(false)
  const [driveEnsuring, setDriveEnsuring] = useState(false)
  const [driveEnsureResult, setDriveEnsureResult] = useState<string | null>(null)
  const [balances, setBalances] = useState<{
    voiceapi:  { balance_chars?: number; balance_text?: string; error?: string }
    voidai:    { balance_usd?: number | null; note?: string }
    wavespeed: { subscription_end?: string | null; note?: string }
  } | null>(null)
  const [wsExpiry, setWsExpiry] = useState<string>(
    localStorage.getItem('vf_wavespeed_expiry') ?? ''
  )
  const [wsExpiryEdit, setWsExpiryEdit] = useState(false)

  // Only real channel configs (exclude example_* templates)
  const allChannelNames = channels
    .map(c => c.name)
    .filter(n => !n.includes('example'))
    .sort()

  const [pForm, setPForm] = useState<PFormState>({
    source_dir:       localStorage.getItem(LS_SOURCE_DIR) ?? '',
    channel:          'config/channels/history.json',
    quality:          'max',
    template:         'auto',
    draft:            false,
    dry_run:          false,
    from_step:        1,
    to_step:          6,
    background_music: false,
    skip_thumbnail:   false,
    burn_subtitles:   false,
    no_ken_burns:     false,
    auto_approve:     false,
    force:            false,
    image_style:      '',
    voice_id:         '',
    master_prompt:    null,
    duration_min:     25,
    duration_max:     30,
    music_volume:     null,
    music_track:      null,
    custom_topic:     '',
    image_backend:    '',
    vision_model:     'gpt-4.1',
  })

  const [bForm, setBForm] = useState<BatchRunRequest>({
    input_dir: localStorage.getItem(LS_INPUT_DIR) ?? '',
    channel:   'config/channels/history.json',
    quality:   'bulk',
    parallel:  1,
    dry_run:   false,
    skip_done: true,
  })

  // ── Multi-topic queue state ────────────────────────────────────────────────
  const DEFAULT_MULTI_ITEM = (): MultiTopicItem => ({
    source_dir:   '',
    channel:      'config/channels/history.json',
    custom_topic: '',
    quality:      'max',
    image_style:  '',
  })

  // Load saved items; fall back to one empty row
  const _savedItems   = lsGet<MultiTopicItem[]>(LS_MULTI_ITEMS, [DEFAULT_MULTI_ITEM()])
  const _savedSettings = lsGet<Record<string, unknown>>(LS_MULTI_SETTINGS, {})
  const _ms = _savedSettings  // shorthand

  const [mQuick, setMQuick]             = useState<boolean>(false)
  const [mItems, setMItems]             = useState<MultiTopicItem[]>(_savedItems.length ? _savedItems : [DEFAULT_MULTI_ITEM()])
  const [mParallel, setMParallel]       = useState<number>((_ms.parallel as number) ?? 2)
  const [mStyle, setMStyle]             = useState<string>((_ms.image_style as string) ?? '')
  const [mDryRun, setMDryRun]           = useState<boolean>((_ms.dry_run as boolean) ?? false)
  const [mDraft, setMDraft]             = useState<boolean>((_ms.draft as boolean) ?? false)
  const [mFromStep, setMFromStep]       = useState<number>((_ms.from_step as number) ?? 1)
  const [mToStep, setMToStep]           = useState<number>((_ms.to_step as number) ?? 6)
  const [mTemplate, setMTemplate]       = useState<string>((_ms.template as string) ?? 'auto')
  const [mDurMin, setMDurMin]           = useState<number>((_ms.duration_min as number) ?? 25)
  const [mDurMax, setMDurMax]           = useState<number>((_ms.duration_max as number) ?? 30)
  // Migrate old localStorage value (stem-only → full path with prompts/ prefix)
  const _rawMaster = (_ms.master_prompt as string | null) ?? null
  const _initMaster = _rawMaster && !_rawMaster.startsWith('prompts/')
    ? null   // discard legacy invalid value
    : _rawMaster
  const [mMaster, setMMaster]           = useState<string | null>(_initMaster)
  const [mVoice, setMVoice]             = useState<string>((_ms.voice_id as string) ?? '')
  const [mMusic, setMMusic]             = useState<boolean>((_ms.background_music as boolean) ?? false)
  const [mMusicVol, setMusicVol]        = useState<number | ''>((_ms.music_volume as number | null) ?? '')
  const [mMusicTrack, setMMusicTrack]   = useState<string | null>((_ms.music_track as string | null) ?? null)
  const [mSubs, setMSubs]               = useState<boolean>((_ms.burn_subtitles as boolean) ?? false)
  const [mSkipThumb, setMSkipThumb]     = useState<boolean>((_ms.skip_thumbnail as boolean) ?? false)
  const [mNoKenBurns, setMNoKenBurns]   = useState<boolean>((_ms.no_ken_burns as boolean) ?? false)
  const [mAutoApprove, setMAutoApprove] = useState<boolean>((_ms.auto_approve as boolean) ?? false)
  const [mForce, setMForce]             = useState<boolean>(false)
  const [mImageBackend, setMImageBackend] = useState<string>((_ms.image_backend as string) ?? '')
  const [mVisionModel, setMVisionModel]   = useState<string>((_ms.vision_model as string) ?? 'gpt-4.1')
  const [mBudget, setMBudget]           = useState<number | ''>((_ms.budget_per_video as number | null) ?? '')

  // Auto-save items to localStorage whenever they change
  useEffect(() => { lsSet(LS_MULTI_ITEMS, mItems) }, [mItems])

  // Auto-save all global settings whenever any changes
  useEffect(() => {
    lsSet(LS_MULTI_SETTINGS, {
      parallel: mParallel, image_style: mStyle, dry_run: mDryRun, draft: mDraft,
      from_step: mFromStep, to_step: mToStep, template: mTemplate,
      duration_min: mDurMin, duration_max: mDurMax, master_prompt: mMaster,
      voice_id: mVoice, background_music: mMusic, music_volume: mMusicVol || null,
      music_track: mMusicTrack, burn_subtitles: mSubs, skip_thumbnail: mSkipThumb,
      no_ken_burns: mNoKenBurns, auto_approve: mAutoApprove,
      image_backend: mImageBackend, vision_model: mVisionModel,
      budget_per_video: mBudget || null,
    })
  }, [mParallel, mStyle, mDryRun, mDraft, mFromStep, mToStep, mTemplate,
      mDurMin, mDurMax, mMaster, mVoice, mMusic, mMusicVol, mMusicTrack,
      mSubs, mSkipThumb, mNoKenBurns, mAutoApprove, mImageBackend, mVisionModel, mBudget])  // eslint-disable-line

  const addMItem  = () => setMItems(prev => [...prev, DEFAULT_MULTI_ITEM()])
  const removeMItem = (i: number) => setMItems(prev => prev.filter((_, idx) => idx !== i))
  const updateMItem = (i: number, patch: Partial<MultiTopicItem>) =>
    setMItems(prev => prev.map((it, idx) => idx === i ? { ...it, ...patch } : it))

  const [submitting, setSubmitting] = useState(false)
  const [appending,  setAppending]  = useState(false)
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

  // Load voices + prompts + channels + project folders + presets
  useEffect(() => {
    api.voices.list().then(setVoices).catch(() => {})
    api.prompts.list().then(setPrompts).catch(() => {})
    api.music.list().then(setMusicTracks).catch(() => {})
    api.channels.list().then(setChannels).catch(() => {})
    api.projects.folders().then(setProjectFolders).catch(() => {})
    api.presets.list().then(setPresets).catch(() => {})
    api.drive.status().then(setDriveStatus).catch(() => {})
    api.status.balances().then(setBalances).catch(() => {})
  }, [])

  async function loadJobs() {
    try { setJobs(await api.jobs.list(100)) }
    catch { /* ignore */ }
    finally { setLoading(false) }
  }

  async function refreshFolderData() {
    setRefreshingFolders(true)
    try {
      const [chs, folders] = await Promise.all([
        api.channels.list(),
        api.projects.folders(),
      ])
      setChannels(chs)
      setProjectFolders(folders)
    } catch { /* ignore */ }
    finally { setRefreshingFolders(false) }
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
    // Image style is required — no channel_config fallback
    if (!pForm.image_style.trim()) {
      setFormError('⚠ Image Style is required. Paste a reference image above or type a style description.')
      return
    }
    setSubmitting(true)
    try {
      const payload: Record<string, unknown> = { ...pForm }
      // image_style is always sent (required field)
      if (!payload.voice_id)            delete payload.voice_id
      if (payload.music_volume == null) delete payload.music_volume
      if (!payload.custom_topic)        delete payload.custom_topic
      if (!payload.image_backend)       payload.image_backend = null   // "" → null → auto from channel config
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

  async function submitMulti(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')

    // ── Quick mode: script + voice + 1 image per item ─────────────────────
    if (mQuick) {
      const valid = mItems.filter(it => (it.custom_topic ?? '').trim())
      if (!valid.length) { setFormError('У Quick mode тема обов\'язкова для кожного рядка'); return }
      setSubmitting(true)
      try {
        const batchReq: QuickBatchRequest = {
          parallel:      mParallel,
          voice_id:      mVoice || null,
          image_backend: mImageBackend || null,
          duration_min:  mDurMin,
          duration_max:  mDurMax,
          items: valid.map(item => ({
            topic:             item.custom_topic!.trim(),
            transcription_url: item.source_dir.trim() || '',
            channel:           item.channel ?? 'config/channels/history.json',
            quality:           item.quality ?? 'max',
          })),
        }
        await api.pipeline.quickBatch(batchReq)
        await loadJobs()
        // Don't clear items — user may want to retry failed jobs or reuse the list
      } catch (err) {
        setFormError(String(err))
      } finally {
        setSubmitting(false)
      }
      return
    }

    // ── Normal mode ───────────────────────────────────────────────────────
    // Each item needs at least source_dir OR custom_topic
    const valid = mItems.filter(it => it.source_dir.trim() || (it.custom_topic ?? '').trim())
    if (!valid.length) { setFormError('Додайте хоча б одне відео (source_dir або тему)'); return }
    const invalid = valid.filter(it => !it.source_dir.trim() && !(it.custom_topic ?? '').trim())
    if (invalid.length) { setFormError('Кожен рядок повинен мати source_dir або тему'); return }
    setSubmitting(true)
    try {
      const body: MultiBatchRequest = {
        items:            valid,
        parallel:         mParallel,
        image_style:      mStyle || undefined,
        dry_run:          mDryRun,
        draft:            mDraft,
        from_step:        mFromStep,
        to_step:          mToStep,
        template:         mTemplate,
        duration_min:     mDurMin,
        duration_max:     mDurMax,
        master_prompt:    mMaster || null,
        voice_id:         mVoice || null,
        background_music: mMusic,
        music_volume:     mMusicVol !== '' ? mMusicVol : null,
        music_track:      mMusicTrack || null,
        burn_subtitles:   mSubs,
        skip_thumbnail:   mSkipThumb,
        no_ken_burns:     mNoKenBurns,
        auto_approve:     mAutoApprove,
        force:            mForce,
        image_backend:    mImageBackend || null,
        vision_model:     mVisionModel || null,
        budget_per_video: mBudget !== '' ? mBudget : null,
      }
      await api.batch.runMulti(body)
      await loadJobs()
      setMItems([DEFAULT_MULTI_ITEM()])  // clear form — avoid re-adding same videos
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function appendMulti(e: React.MouseEvent) {
    e.preventDefault()
    setFormError('')
    const valid = mItems.filter(it => it.source_dir.trim() || (it.custom_topic ?? '').trim())
    if (!valid.length) { setFormError('Додайте хоча б одне відео'); return }
    setAppending(true)
    try {
      const body: MultiBatchRequest = {
        items:            valid,
        parallel:         mParallel,
        image_style:      mStyle || undefined,
        dry_run:          mDryRun,
        draft:            mDraft,
        from_step:        mFromStep,
        to_step:          mToStep,
        template:         mTemplate,
        duration_min:     mDurMin,
        duration_max:     mDurMax,
        master_prompt:    mMaster || null,
        voice_id:         mVoice || null,
        background_music: mMusic,
        music_volume:     mMusicVol !== '' ? mMusicVol : null,
        music_track:      mMusicTrack || null,
        burn_subtitles:   mSubs,
        skip_thumbnail:   mSkipThumb,
        no_ken_burns:     mNoKenBurns,
        auto_approve:     mAutoApprove,
        force:            mForce,
        image_backend:    mImageBackend || null,
        vision_model:     mVisionModel || null,
        budget_per_video: mBudget !== '' ? mBudget : null,
      }
      await api.batch.appendMulti(body)
      await loadJobs()
      setMItems([DEFAULT_MULTI_ITEM()])  // clear form after appending
    } catch (err) {
      setFormError(String(err))
    } finally {
      setAppending(false)
    }
  }

  const activeJobs = jobs.filter((j) => j.status === 'running' || j.status === 'queued')
  const recentJobs = jobs.filter((j) => j.status !== 'running' && j.status !== 'queued').slice(0, 20)

  function handleTranscriberSelect(dir: string) {
    if (tab === 'multi') {
      // Multi-topic mode: add path to queue (fill first empty slot or append new)
      setMItems(prev => {
        const emptyIdx = prev.findIndex(it => !it.source_dir.trim() && !it.custom_topic?.trim())
        if (emptyIdx >= 0) {
          return prev.map((it, i) => i === emptyIdx ? { ...it, source_dir: dir } : it)
        }
        return [...prev, { ...DEFAULT_MULTI_ITEM(), source_dir: dir }]
      })
      return
    }
    // Single mode: if source_dir already filled — switch to multi and move existing + new
    const currentSingle = pForm.source_dir.trim()
    if (currentSingle) {
      setMItems(prev => {
        // Check if current single path already in multi list
        const hasExisting = prev.some(it => it.source_dir.trim() === currentSingle)
        let items = hasExisting ? [...prev] : [{ ...DEFAULT_MULTI_ITEM(), source_dir: currentSingle }, ...prev.filter(it => it.source_dir.trim() || it.custom_topic?.trim())]
        // Add new dir
        const emptyIdx = items.findIndex(it => !it.source_dir.trim() && !it.custom_topic?.trim())
        if (emptyIdx >= 0) {
          items = items.map((it, i) => i === emptyIdx ? { ...it, source_dir: dir } : it)
        } else {
          items = [...items, { ...DEFAULT_MULTI_ITEM(), source_dir: dir }]
        }
        return items.length ? items : [DEFAULT_MULTI_ITEM()]
      })
      setTab('multi')
      return
    }
    // First path — fill single mode
    updateSourceDir(dir)
    setTab('pipeline')
    // Scroll to source dir input
    setTimeout(() => sourceRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 100)
  }

  function applyPreset(id: string) {
    setSelectedPresetId(id)
    localStorage.setItem(LS_PRESET_ID, id)
    if (!id) return
    const p = presets.find(pr => pr.id === id)
    if (!p) return
    // Apply to pipeline form
    setPForm(f => ({
      ...f,
      channel:          p.channel,
      quality:          p.quality,
      duration_min:     p.duration_min,
      duration_max:     p.duration_max,
      template:         p.template,
      skip_thumbnail:   p.skip_thumbnail,
      auto_approve:     p.auto_approve,
      image_backend:    p.image_backend,
      background_music: p.background_music,
      burn_subtitles:   p.burn_subtitles,
      no_ken_burns:     p.no_ken_burns,
      master_prompt:    p.master_prompt,
      image_style:      p.image_style,
      voice_id:         p.voice_id,
      music_volume:     p.music_volume,
      vision_model:     p.vision_model,
    }))
    // Apply to multi form
    setMParallel(p.parallel)
    setMDurMin(p.duration_min)
    setMDurMax(p.duration_max)
    setMTemplate(p.template)
    setMSkipThumb(p.skip_thumbnail)
    setMAutoApprove(p.auto_approve)
    setMImageBackend(p.image_backend)
    setMMusic(p.background_music)
    setMSubs(p.burn_subtitles)
    setMNoKenBurns(p.no_ken_burns)
    setMMaster(p.master_prompt)
    setMStyle(p.image_style)
    setMVoice(p.voice_id)
    setMusicVol(p.music_volume ?? '')
    setMVisionModel(p.vision_model)
    // Update channel on all multi items
    setMItems(prev => prev.map(it => ({ ...it, channel: p.channel, quality: p.quality })))
  }

  async function handleAddToQueue(dir: string) {
    // Add single video to active queue without touching the form
    try {
      const channel = pForm.channel || 'config/channels/history.json'
      await api.batch.appendMulti({
        items: [{ source_dir: dir, channel, image_style: '', quality: 'max' }],
        parallel:         mParallel,
        image_style:      mStyle || '',
        dry_run:          mDryRun,
        draft:            mDraft,
        from_step:        mFromStep,
        to_step:          mToStep,
        template:         mTemplate,
        duration_min:     mDurMin,
        duration_max:     mDurMax,
        master_prompt:    mMaster || null,
        voice_id:         mVoice || null,
        background_music: mMusic,
        music_volume:     mMusicVol !== '' ? mMusicVol : null,
        music_track:      mMusicTrack || null,
        burn_subtitles:   mSubs,
        skip_thumbnail:   mSkipThumb,
        no_ken_burns:     mNoKenBurns,
        auto_approve:     mAutoApprove,
        force:            mForce,
        image_backend:    mImageBackend || null,
        vision_model:     mVisionModel || null,
        budget_per_video: mBudget !== '' ? mBudget : null,
      })
      await loadJobs()
    } catch (err) {
      setFormError(String(err))
    }
  }

  return (
    <div className="space-y-6">
      {/* Transcriber integration */}
      <TranscriberPanel onSelectDir={handleTranscriberSelect} onAddToQueue={handleAddToQueue} pipelineSettings={pForm} />

      {/* Google Drive status banner */}
      {driveStatus && (
        <div className={`flex items-center gap-3 px-4 py-2.5 rounded-lg border text-xs ${
          driveStatus.authenticated && driveStatus.root_folder_id
            ? 'bg-green-900/20 border-green-800/50 text-green-300'
            : 'bg-gray-800 border-gray-700 text-gray-400'
        }`}>
          <span>☁ Google Drive:</span>
          {driveStatus.authenticated && driveStatus.root_folder_id ? (
            <>
              <span className="text-green-400">підключено</span>
              <button
                onClick={async () => {
                  setDriveEnsuring(true)
                  setDriveEnsureResult(null)
                  try {
                    const res = await api.drive.ensureChannelFolders()
                    setDriveEnsureResult(`✓ ${res.channels.length} папок готово`)
                  } catch (e) {
                    setDriveEnsureResult(`✗ ${String(e)}`)
                  } finally {
                    setDriveEnsuring(false)
                  }
                }}
                disabled={driveEnsuring}
                className="px-2 py-0.5 bg-green-800 hover:bg-green-700 text-green-200 rounded disabled:opacity-50 transition-colors"
              >
                {driveEnsuring ? '⏳ Створюю…' : '📁 Папки каналів'}
              </button>
              {driveEnsureResult && <span className="text-green-300">{driveEnsureResult}</span>}
            </>
          ) : driveStatus.authenticated ? (
            <span className="text-amber-400">авторизовано, але GDRIVE_ROOT_FOLDER_ID не встановлено у .env</span>
          ) : (
            <>
              <span>не авторизовано</span>
              <button
                onClick={async () => {
                  setDriveAuthing(true)
                  try {
                    await api.drive.auth()
                    const s = await api.drive.status()
                    setDriveStatus(s)
                  } catch { /* ignore */ }
                  finally { setDriveAuthing(false) }
                }}
                disabled={driveAuthing}
                className="px-2 py-0.5 bg-blue-700 hover:bg-blue-600 text-white rounded disabled:opacity-50 transition-colors"
              >
                {driveAuthing ? 'Відкриваю браузер…' : 'Авторизувати'}
              </button>
            </>
          )}
          <button
            onClick={async () => { const s = await api.drive.status(); setDriveStatus(s) }}
            className="ml-auto text-gray-500 hover:text-gray-300 transition-colors"
            title="Оновити статус"
          >↺</button>
        </div>
      )}

      {/* Service balances */}
      {balances && (
        <div className="flex items-center gap-4 px-4 py-2 rounded-lg bg-gray-800/60 border border-gray-700/50 text-xs flex-wrap">
          <span className="text-gray-500 shrink-0">Баланси:</span>
          {/* VoiceAPI */}
          <div className="flex items-center gap-1.5">
            <span className="text-gray-500">🎙</span>
            {balances.voiceapi.error ? (
              <span className="text-red-400">Помилка</span>
            ) : (
              <span className="text-green-300" title={balances.voiceapi.balance_text}>
                {balances.voiceapi.balance_chars != null
                  ? `${(balances.voiceapi.balance_chars / 1000).toFixed(0)}k символів`
                  : '—'}
              </span>
            )}
            <span className="text-gray-600">VoiceAPI</span>
          </div>
          {/* VoidAI */}
          <div className="flex items-center gap-1.5">
            <span className="text-gray-500">🤖</span>
            <span className="text-gray-500 italic text-[10px]">баланс недоступний</span>
            <span className="text-gray-600">VoidAI</span>
          </div>
          {/* WaveSpeed — manual expiry date */}
          <div className="flex items-center gap-1.5">
            <span className="text-gray-500">🖼</span>
            {wsExpiryEdit ? (
              <input
                type="date"
                autoFocus
                defaultValue={wsExpiry}
                onBlur={(e) => {
                  const v = e.target.value
                  setWsExpiry(v)
                  localStorage.setItem('vf_wavespeed_expiry', v)
                  setWsExpiryEdit(false)
                }}
                onKeyDown={(e) => { if (e.key === 'Escape') setWsExpiryEdit(false) }}
                className="bg-gray-900 border border-gray-600 rounded px-1 py-0.5 text-xs text-white focus:outline-none focus:border-blue-500"
              />
            ) : wsExpiry ? (() => {
              const days = Math.ceil((new Date(wsExpiry).getTime() - Date.now()) / 86400000)
              const color = days <= 3 ? 'text-red-400' : days <= 7 ? 'text-yellow-400' : 'text-green-300'
              return (
                <button onClick={() => setWsExpiryEdit(true)} className={`${color} hover:underline`} title="Натисни щоб змінити дату">
                  {days > 0 ? `ще ${days}д` : `прострочено ${Math.abs(days)}д тому`}
                </button>
              )
            })() : (
              <button onClick={() => setWsExpiryEdit(true)} className="text-gray-600 hover:text-gray-400 italic" title="Вкажи дату закінчення підписки">
                вкажи термін
              </button>
            )}
            <span className="text-gray-600">WaveSpeed</span>
          </div>
          <button
            onClick={() => api.status.balances().then(setBalances).catch(() => {})}
            className="ml-auto text-gray-600 hover:text-gray-400 transition-colors"
            title="Оновити баланси"
          >↺</button>
        </div>
      )}

      {/* Launch form */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <div className="flex gap-2 mb-4 flex-wrap items-center">
          {(['pipeline', 'batch', 'multi'] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                tab === t ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              {t === 'pipeline' ? 'Single Video' : t === 'batch' ? 'Batch Dir' : 'Multi-Topic'}
            </button>
          ))}

          {/* Preset selector */}
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-gray-500">Preset:</span>
            {presets.length > 0 ? (
              <select
                value={selectedPresetId}
                onChange={e => applyPreset(e.target.value)}
                className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
              >
                <option value="">— none —</option>
                {presets.map(p => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            ) : (
              <span className="text-xs text-gray-600 italic">немає — створи у вкладці «Presets»</span>
            )}
          </div>
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

            {/* Video topic */}
            <label className="space-y-1 block">
              <span className="text-xs text-gray-400">
                Тема відео
                <Tip text="Тема нового відео. Якщо вказано — LLM пише сценарій на цю тему, використовуючи референс тільки як структурний зразок. Порожньо = тема береться з назви референс-відео." />
              </span>
              <input
                value={pForm.custom_topic}
                onChange={(e) => setPForm({ ...pForm, custom_topic: e.target.value })}
                placeholder="Наприклад: Як Стоїцизм рятує від тривоги"
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </label>

            {/* Channel + Quality */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Канал</span>
                <select value={pForm.channel}
                  onChange={(e) => setPForm({ ...pForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {allChannelNames.length > 0
                    ? allChannelNames.map(name => {
                        const hasConfig = channels.some(c => c.name === name)
                        const folder = projectFolders.find(f => f.name === name)
                        const suffix = !folder ? ' — нова'
                          : folder.video_count > 0 ? ` — ${folder.video_count} відео` : ' — порожньо'
                        return (
                          <option key={name} value={hasConfig ? `config/channels/${name}.json` : ''} disabled={!hasConfig}>
                            {name}{suffix}{!hasConfig ? ' ⚠ немає конфіга' : ''}
                          </option>
                        )
                      })
                    : <option value={pForm.channel}>{pForm.channel}</option>
                  }
                </select>
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
                <div className="space-y-1.5 ml-1">
                  {/* Volume */}
                  <div className="flex items-center gap-2">
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
                  {/* Track selector */}
                  {musicTracks.length > 0 && (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-gray-400 shrink-0">Трек:</span>
                      <select
                        value={pForm.music_track ?? ''}
                        onChange={(e) =>
                          setPForm((f) => ({ ...f, music_track: e.target.value || null }))
                        }
                        className="flex-1 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                      >
                        <option value="">Авто (з налаштувань каналу)</option>
                        {musicTracks.map((t) => (
                          <option key={t.path} value={t.path}>
                            {t.rel_path} ({t.size_mb} MB)
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
              )}
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.skip_thumbnail}
                  onChange={(e) => setPForm({ ...pForm, skip_thumbnail: e.target.checked })}
                  className="accent-blue-500" />
                <span>Skip thumbnail</span>
                <Tip text="Пропустити генерацію thumbnail (Step 5). Корисно коли thumbnail вже є або потрібно швидко отримати відео." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.burn_subtitles}
                  onChange={(e) => setPForm({ ...pForm, burn_subtitles: e.target.checked })}
                  className="accent-blue-500" />
                <span>Burn subtitles</span>
                <Tip text="Записати субтитри у відео (крок 4 повинен бути виконаний). Вимкніть для відео без субтитрів." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.no_ken_burns}
                  onChange={(e) => setPForm({ ...pForm, no_ken_burns: e.target.checked })}
                  className="accent-blue-500" />
                <span>No Ken Burns</span>
                <Tip text="Статичний слайдшоу замість Ken Burns анімації. Набагато швидший рендер — рекомендовано для довгих відео (25+ хв)." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.auto_approve}
                  onChange={(e) => setPForm({ ...pForm, auto_approve: e.target.checked })}
                  className="accent-green-500" />
                <span>Auto-approve</span>
                <Tip text="Автоматично апрувити сценарій та картинки якщо: є хук, тривалість в межах, всі картинки ОК. Інакше — ручний ревʼю." />
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.force}
                  onChange={(e) => setPForm({ ...pForm, force: e.target.checked })}
                  className="accent-red-500" />
                <span>Force regen</span>
                <Tip text="Видалити все (транскрипцію + проект) і згенерувати з нуля. Корисно якщо попередня генерація вийшла коротка або зіпсована." />
              </label>
              {/* Image backend selector */}
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">
                  Image backend
                  <Tip text="Провайдер генерації картинок: Channel config (auto) — з налаштувань каналу, WaveSpeed (дешевий), VoiceImage (voiceapi.csv666.ru), VoidAI (резервний, дорогий)." />
                </span>
                <select
                  value={pForm.image_backend}
                  onChange={(e) => setPForm({ ...pForm, image_backend: e.target.value })}
                  className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  <option value="">Channel config (auto)</option>
                  <option value="wavespeed">WaveSpeed</option>
                  <option value="voiceimage">VoiceImage (voiceapi.csv666.ru)</option>
                  <option value="betatest">BetaTest (legacy alias)</option>
                  <option value="voidai">VoidAI only</option>
                </select>
              </label>
              {/* Vision model selector */}
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">
                  Vision model (image analysis)
                  <Tip text="Модель для аналізу та валідації картинок. gpt-4.1 — точніша, gpt-4.1-mini — дешевша та швидша." />
                </span>
                <select
                  value={pForm.vision_model}
                  onChange={(e) => setPForm({ ...pForm, vision_model: e.target.value })}
                  className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  <option value="gpt-4.1">gpt-4.1 (default, accurate)</option>
                  <option value="gpt-4.1-mini">gpt-4.1-mini (faster, cheaper)</option>
                </select>
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

        ) : tab === 'batch' ? (
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
                <span className="text-xs text-gray-400">Канал</span>
                <select value={bForm.channel}
                  onChange={(e) => setBForm({ ...bForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {allChannelNames.length > 0
                    ? allChannelNames.map(name => {
                        const hasConfig = channels.some(c => c.name === name)
                        const folder = projectFolders.find(f => f.name === name)
                        const suffix = !folder ? ' — нова'
                          : folder.video_count > 0 ? ` — ${folder.video_count} відео` : ' — порожньо'
                        return (
                          <option key={name} value={hasConfig ? `config/channels/${name}.json` : ''} disabled={!hasConfig}>
                            {name}{suffix}{!hasConfig ? ' ⚠ немає конфіга' : ''}
                          </option>
                        )
                      })
                    : <option value={bForm.channel}>{bForm.channel}</option>
                  }
                </select>
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

        ) : (

          /* ── Multi-Topic Queue ───────────────────────────────────────────── */
          <form onSubmit={submitMulti} className="space-y-4">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-3">
                <p className="text-xs text-gray-400">
                  Черга відео з різними темами та каналами. Всі запускаються паралельно (до
                  {' '}<strong className="text-gray-300">{mParallel}</strong> одночасно).
                  Кожне відео відображається як окремий Job.
                </p>
                {/* Quick mode toggle */}
                <label className="flex items-center gap-1.5 shrink-0 cursor-pointer select-none">
                  <input type="checkbox" checked={mQuick} onChange={(e) => setMQuick(e.target.checked)}
                    className="accent-yellow-500" />
                  <span className="text-xs text-yellow-400 font-medium whitespace-nowrap">⚡ Quick mode</span>
                </label>
              </div>
              <button
                type="button"
                onClick={() => api.fs.open('projects').catch(() => {})}
                className="shrink-0 flex items-center gap-1 text-xs text-gray-400 hover:text-white border border-gray-600 hover:border-gray-400 rounded px-2 py-1 transition-colors"
                title="Відкрити папку projects/ у Провіднику Windows"
              >
                📁 projects/
              </button>
            </div>
            {mQuick && (
              <p className="text-[11px] text-yellow-600 bg-yellow-950/30 rounded px-2 py-1">
                ⚡ Quick mode: сценарій + озвучка + 1 картинка. Без монтажу відео.
                Колонка «Посилання» приймає YouTube URL або шлях до транскрипції (необов'язково).
              </p>
            )}

            {/* Per-item rows */}
            <div className="space-y-2">
              {mItems.map((item, i) => (
                <div key={i} className="grid gap-2 p-2 bg-gray-900 rounded border border-gray-700"
                  style={{ gridTemplateColumns: '1fr 1fr 1fr auto auto' }}>

                  {/* Source dir / Transcription URL */}
                  <div className="space-y-0.5">
                    {i === 0 && (
                      <div className="text-[10px] text-gray-500">
                        {mQuick
                          ? <span>Посилання <span className="text-gray-600">(YouTube URL або шлях, необов'язково)</span></span>
                          : <span>Source dir <span className="text-gray-600">(або тільки тема)</span></span>
                        }
                      </div>
                    )}
                    <input
                      value={item.source_dir}
                      onChange={(e) => updateMItem(i, { source_dir: e.target.value })}
                      onPaste={(e) => { e.preventDefault(); updateMItem(i, { source_dir: e.clipboardData.getData('text').trim() }) }}
                      placeholder={mQuick ? 'https://youtube.com/... або D:\\transcripts\\...' : "D:/output/Video Title (необов'язково)"}
                      className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                    />
                  </div>

                  {/* Custom topic */}
                  <div className="space-y-0.5">
                    {i === 0 && (
                      <div className="text-[10px] text-gray-500">
                        {mQuick
                          ? <span className="text-yellow-600">Тема *</span>
                          : <span>Тема <span className="text-gray-600">(обов'язково без source_dir)</span></span>
                        }
                      </div>
                    )}
                    <input
                      value={item.custom_topic ?? ''}
                      onChange={(e) => updateMItem(i, { custom_topic: e.target.value })}
                      placeholder={mQuick ? 'How the Roman Empire collapsed' : 'Назва теми для відео'}
                      className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                    />
                  </div>

                  {/* Channel / Folder */}
                  <div className="space-y-0.5">
                    {i === 0 && (
                      <div className="text-[10px] text-gray-500 flex items-center gap-1">
                        Папка (канал)
                        <button
                          type="button"
                          onClick={refreshFolderData}
                          disabled={refreshingFolders}
                          className="text-gray-600 hover:text-blue-400 disabled:opacity-30 transition-colors"
                          title="Оновити список каналів і папок"
                        >{refreshingFolders ? '…' : '↻'}</button>
                      </div>
                    )}
                    <select
                      value={item.channel ?? 'config/channels/history.json'}
                      onChange={(e) => updateMItem(i, { channel: e.target.value })}
                      className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                    >
                      {allChannelNames.length > 0
                        ? allChannelNames.map(name => {
                            const hasConfig = channels.some(c => c.name === name)
                            const folder = projectFolders.find(f => f.name === name)
                            const suffix = !folder
                              ? ' — нова'
                              : folder.video_count > 0
                                ? ` — ${folder.video_count} відео`
                                : ' — порожньо'
                            return (
                              <option
                                key={name}
                                value={hasConfig ? `config/channels/${name}.json` : ''}
                                disabled={!hasConfig}
                              >
                                {name}{suffix}{!hasConfig ? ' ⚠ немає конфіга' : ''}
                              </option>
                            )
                          })
                        : <option value={item.channel ?? 'config/channels/history.json'}>
                            {(item.channel ?? '').replace('config/channels/', '').replace('.json', '') || 'history'}
                          </option>
                      }
                    </select>
                  </div>

                  {/* Quality */}
                  <div className="space-y-0.5">
                    {i === 0 && <div className="text-[10px] text-gray-500">Q</div>}
                    <select
                      value={item.quality ?? 'max'}
                      onChange={(e) => updateMItem(i, { quality: e.target.value })}
                      className="bg-gray-800 border border-gray-600 rounded px-1 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                    >
                      <option value="max">max</option>
                      <option value="high">high</option>
                      <option value="balanced">balanced</option>
                      <option value="bulk">bulk</option>
                      <option value="test">test</option>
                    </select>
                  </div>

                  {/* Remove */}
                  <div className={i === 0 ? 'pt-4' : ''}>
                    <button type="button" onClick={() => removeMItem(i)}
                      disabled={mItems.length === 1}
                      className="text-gray-500 hover:text-red-400 disabled:opacity-30 text-lg leading-none px-1"
                      title="Видалити рядок"
                    >×</button>
                  </div>
                </div>
              ))}
            </div>

            {/* Add row */}
            <button type="button" onClick={addMItem}
              className="text-xs text-blue-400 hover:text-blue-300 border border-blue-800 hover:border-blue-600 rounded px-3 py-1 transition-colors"
            >
              + Додати відео
            </button>

            {/* ── Global settings ───────────────────────────────────────── */}
            <div className="border-t border-gray-700 pt-3 space-y-3">

              {/* Template + Quality (global default, per-item can override quality) */}
              <div className="grid grid-cols-2 gap-3">
                <label className="space-y-1">
                  <span className="text-xs text-gray-400">
                    Template
                    <Tip text="Формат відео. Auto — LLM сам обирає." />
                  </span>
                  <DescSelect value={mTemplate} onChange={setMTemplate} options={TEMPLATE_OPTS} />
                </label>
                <label className="space-y-1">
                  <span className="text-xs text-gray-400">
                    Parallel workers
                    <Tip text="Скільки відео генерується одночасно. 1 = послідовно, 2–4 = паралельно." />
                  </span>
                  <input type="number" min={1} max={8} value={mParallel}
                    onChange={(e) => setMParallel(Number(e.target.value))}
                    className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                  />
                </label>
              </div>

              {/* from_step / to_step */}
              <div className="space-y-1">
                <span className="text-xs text-gray-400">Steps</span>
                <div className="flex flex-wrap gap-1 mb-1">
                  {[
                    { label: 'All',       f: 1, t: 6 },
                    { label: '1 Script',  f: 1, t: 1 },
                    { label: '2 Imgs+TTS',f: 2, t: 2 },
                    { label: '3 Subs',    f: 3, t: 3 },
                    { label: '4 Video',   f: 4, t: 4 },
                    { label: '5 Thumb',   f: 5, t: 5 },
                    { label: '6 Meta',    f: 6, t: 6 },
                  ].map(({ label, f, t }) => {
                    const active = mFromStep === f && mToStep === t
                    return (
                      <button key={label} type="button"
                        onClick={() => { setMFromStep(f); setMToStep(t) }}
                        className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${active ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'}`}
                      >{label}</button>
                    )
                  })}
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-500 shrink-0">from</span>
                  <DescSelect value={mFromStep}
                    onChange={(v) => { const n = Number(v); setMFromStep(n); setMToStep(t => Math.max(t, n)) }}
                    options={STEP_OPTS}
                  />
                  <span className="text-xs text-gray-500 shrink-0">to</span>
                  <DescSelect value={mToStep}
                    onChange={(v) => { const n = Number(v); setMToStep(n); setMFromStep(f => Math.min(f, n)) }}
                    options={STEP_OPTS}
                  />
                </div>
              </div>

              {/* Master prompt */}
              {prompts.length > 0 && (
                <label className="space-y-1 block">
                  <span className="text-xs text-gray-400">
                    Master prompt
                    <Tip text="Перевизначає промпт каналу для всіх відео." />
                  </span>
                  <select value={mMaster ?? ''}
                    onChange={(e) => setMMaster(e.target.value || null)}
                    className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                  >
                    <option value="">(з каналу)</option>
                    {prompts.map((p) => (
                      <option key={p.name} value={`prompts/${p.filename}`}>{p.name}</option>
                    ))}
                  </select>
                </label>
              )}

              {/* Duration */}
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400 shrink-0">Тривалість:</span>
                <input type="number" min={1} max={240} value={mDurMin}
                  onChange={(e) => { const v = Math.max(1, parseInt(e.target.value) || 1); setMDurMin(v); setMDurMax(d => Math.max(d, v)) }}
                  className="w-16 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500">–</span>
                <input type="number" min={1} max={240} value={mDurMax}
                  onChange={(e) => { const v = Math.max(mDurMin, parseInt(e.target.value) || mDurMin); setMDurMax(v) }}
                  className="w-16 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 text-center"
                />
                <span className="text-xs text-gray-500">хв ≈ {mDurMin * 140}–{mDurMax * 150} слів</span>
              </div>

              {/* Voice */}
              {voices.length > 0 && (
                <label className="space-y-1 block">
                  <span className="text-xs text-gray-400">
                    Voice
                    <Tip text="Голос для всіх відео. Порожньо = з налаштувань каналу." />
                  </span>
                  <select value={mVoice}
                    onChange={(e) => setMVoice(e.target.value)}
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
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">
                  Image style (глобальний)
                  <Tip text="Стиль зображень для всіх відео. Перевизначається per-item style." />
                </span>
                <input value={mStyle} onChange={(e) => setMStyle(e.target.value)}
                  placeholder="cinematic, documentary, 8k..."
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                />
              </label>

              {/* Budget */}
              <label className="space-y-1 block">
                <span className="text-xs text-gray-400">Budget per video USD (optional)</span>
                <input type="number" step="0.01" placeholder="e.g. 5.00"
                  value={mBudget}
                  onChange={(e) => setMBudget(e.target.value ? Number(e.target.value) : '')}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                />
              </label>

              {/* Checkboxes */}
              <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-gray-300">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mDraft}
                    onChange={(e) => setMDraft(e.target.checked)} className="accent-blue-500" />
                  <span>Draft (480p)</span>
                  <Tip text="Генерує відео 480p без ефектів. Швидше для перевірки структури." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mDryRun}
                    onChange={(e) => setMDryRun(e.target.checked)} className="accent-blue-500" />
                  <span>Dry run</span>
                  <Tip text="Рахує вартість без API викликів." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mMusic}
                    onChange={(e) => setMMusic(e.target.checked)} className="accent-blue-500" />
                  <span>Background music</span>
                  <Tip text="Фонова музика під голос." />
                </label>
                {mMusic && (
                  <>
                    <div className="flex items-center gap-2 ml-1">
                      <span className="text-xs text-gray-400 shrink-0">Гучність dB:</span>
                      <input type="number" min={-60} max={-10} step={1}
                        value={mMusicVol}
                        onChange={(e) => setMusicVol(e.target.value === '' ? '' : Number(e.target.value))}
                        placeholder="-28"
                        className="w-16 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 text-center"
                      />
                    </div>
                    {musicTracks.length > 0 && (
                      <div className="flex items-center gap-2 ml-1">
                        <span className="text-xs text-gray-400 shrink-0">Трек:</span>
                        <select value={mMusicTrack ?? ''}
                          onChange={(e) => setMMusicTrack(e.target.value || null)}
                          className="bg-gray-900 border border-gray-600 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                        >
                          <option value="">(random)</option>
                          {musicTracks.map((t) => (
                            <option key={t.path} value={t.path}>{t.name}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </>
                )}
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mSubs}
                    onChange={(e) => setMSubs(e.target.checked)} className="accent-blue-500" />
                  <span>Burn subtitles</span>
                  <Tip text="Вписати субтитри у відео." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mSkipThumb}
                    onChange={(e) => setMSkipThumb(e.target.checked)} className="accent-blue-500" />
                  <span>Skip thumbnail</span>
                  <Tip text="Не генерувати thumbnail (Step 5)." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mNoKenBurns}
                    onChange={(e) => setMNoKenBurns(e.target.checked)} className="accent-blue-500" />
                  <span>No Ken Burns</span>
                  <Tip text="Статичний слайдшоу замість анімації. Набагато швидший рендер." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mAutoApprove}
                    onChange={(e) => setMAutoApprove(e.target.checked)} className="accent-green-500" />
                  <span>Auto-approve</span>
                  <Tip text="Автоматично апрувити сценарій та картинки якщо: є хук, тривалість в межах, всі картинки ОК." />
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input type="checkbox" checked={mForce}
                    onChange={(e) => setMForce(e.target.checked)} className="accent-red-500" />
                  <span>Force regen</span>
                  <Tip text="Видалити все (транскрипцію + проект) і згенерувати з нуля." />
                </label>
                {/* Image backend */}
                <label className="space-y-1 block">
                  <span className="text-xs text-gray-400">
                    Image backend
                    <Tip text="Провайдер генерації картинок для всіх відео черги." />
                  </span>
                  <select value={mImageBackend}
                    onChange={(e) => setMImageBackend(e.target.value)}
                    className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-purple-500"
                  >
                    <option value="">Channel config (auto)</option>
                    <option value="wavespeed">WaveSpeed</option>
                    <option value="voiceimage">VoiceImage (voiceapi.csv666.ru)</option>
                    <option value="betatest">BetaTest (legacy alias)</option>
                    <option value="voidai">VoidAI only</option>
                  </select>
                </label>
                {/* Vision model */}
                <label className="space-y-1 block">
                  <span className="text-xs text-gray-400">
                    Vision model (image analysis)
                    <Tip text="Модель для аналізу картинок. gpt-4.1-mini — дешевша для великих черг." />
                  </span>
                  <select value={mVisionModel}
                    onChange={(e) => setMVisionModel(e.target.value)}
                    className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-purple-500"
                  >
                    <option value="gpt-4.1">gpt-4.1 (default, accurate)</option>
                    <option value="gpt-4.1-mini">gpt-4.1-mini (faster, cheaper)</option>
                  </select>
                </label>
              </div>
            </div>

            {formError && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>}
            <div className="flex gap-2">
              <button type="submit" disabled={submitting || appending}
                className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
              >
                {submitting
                  ? 'Starting…'
                  : mQuick
                    ? `⚡ Quick: ${mItems.filter(i => (i.custom_topic ?? '').trim()).length} відео`
                    : `🚀 Запустити чергу (${mItems.filter(i => i.source_dir.trim() || (i.custom_topic ?? '').trim()).length} відео)`
                }
              </button>
              <button type="button" onClick={appendMulti} disabled={submitting || appending}
                title="Додати до існуючої черги — нові відео поділяють ліміт паралельності з запущеними"
                className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
              >
                {appending
                  ? 'Додаємо…'
                  : `➕ Додати до черги`
                }
              </button>
            </div>
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
