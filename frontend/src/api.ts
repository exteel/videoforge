/** VideoForge API client — thin wrapper around fetch */

const BASE = '/api'

// ── Auth key (set by AuthContext on login) ─────────────────────────────────────
let _apiKey = ''
export function setApiKey(key: string) { _apiKey = key }

export interface Job {
  job_id: string
  kind: 'pipeline' | 'batch'
  status: 'queued' | 'running' | 'waiting_review' | 'done' | 'failed' | 'cancelled'
  source: string
  source_dir: string   // full path to transcriber output dir
  project_dir: string  // full path to videoforge project output dir
  channel: string
  quality: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  elapsed: number | null
  step: number
  step_name: string
  pct: number          // 0–100 real progress from backend
  error: string
  logs: string[]
  db_video_id: number | null
  review_stage: string | null  // 'script' | 'images' | null
}

export interface Video {
  id: number
  status: string
  source_title: string | null
  source_dir: string
  channel: string
  quality_preset: string
  template: string | null
  created_at: string
  elapsed_seconds: number | null
  youtube_url: string | null
  error_message: string | null
}

export interface CostEntry {
  step: string
  model: string
  input_tokens: number
  output_tokens: number
  units: number
  unit_label: string
  cost_usd: number
  recorded_at: string
}

export interface VideoDetail {
  video: Record<string, unknown>
  costs: CostEntry[]
  total_cost_usd: number
}

export interface Stats {
  total_videos: number
  done: number
  failed: number
  running: number
  avg_elapsed: number | null
  cost_total_usd: number
  by_model: { model: string; total: number; calls: number }[]
  by_preset: { quality_preset: string; total: number; done: number }[]
}

export interface ScriptBlock {
  id: string
  order: number
  type: 'intro' | 'section' | 'cta' | 'outro'
  narration: string
  image_prompt: string
  animation: string
  timestamp_label: string
  audio_duration: number | null
}

export interface Script {
  title: string
  description: string
  tags: string[]
  language: string
  niche: string
  blocks: ScriptBlock[]
  thumbnail_prompt: string
  channel_config: { name: string; voice_id: string; image_style: string }
}

export interface PipelineRunRequest {
  source_dir: string
  channel?: string
  quality?: string
  template?: string
  draft?: boolean
  from_step?: number
  to_step?: number
  budget?: number | null
  langs?: string[] | null
  dry_run?: boolean
  background_music?: boolean
  no_ken_burns?: boolean
  skip_thumbnail?: boolean
  burn_subtitles?: boolean
  image_style?: string | null
  voice_id?: string | null
  master_prompt?: string | null
  duration_min?: number | null
  duration_max?: number | null
  music_volume?: number | null
  music_track?: string | null
  custom_topic?: string | null
  image_backend?: string | null
  vision_model?: string | null
  auto_approve?: boolean
  force?: boolean
}

export interface QuickRunRequest {
  topic: string
  transcription_url?: string
  channel?: string
  quality?: string
  voice_id?: string | null
  image_backend?: string | null
  duration_min?: number | null
  duration_max?: number | null
  force?: boolean
}

export interface QuickBatchItem {
  topic: string
  transcription_url?: string
  channel?: string
  quality?: string
}

export interface QuickBatchRequest {
  items: QuickBatchItem[]
  parallel?: number
  voice_id?: string | null
  image_backend?: string | null
  duration_min?: number | null
  duration_max?: number | null
  force?: boolean
}

export interface MusicTrack {
  name: string      // stem without extension
  filename: string  // filename.ext
  rel_path: string  // relative path inside assets/music/
  path: string      // absolute path — send as music_track
  size_mb: number
}

export interface BatchRunRequest {
  input_dir: string
  channel?: string
  quality?: string
  parallel?: number
  draft?: boolean
  from_step?: number
  budget_per_video?: number | null
  budget_total?: number | null
  skip_done?: boolean
  dry_run?: boolean
}

export interface MultiTopicItem {
  source_dir: string
  channel?: string
  custom_topic?: string
  quality?: string
  image_style?: string
}

export interface MultiBatchRequest {
  items: MultiTopicItem[]
  parallel?: number
  image_style?: string
  dry_run?: boolean
  draft?: boolean
  from_step?: number
  to_step?: number
  budget_per_video?: number | null
  // Script
  template?: string
  duration_min?: number
  duration_max?: number
  master_prompt?: string | null
  // Voice / audio
  voice_id?: string | null
  background_music?: boolean
  music_volume?: number | null
  music_track?: string | null
  burn_subtitles?: boolean
  // Video
  skip_thumbnail?: boolean
  no_ken_burns?: boolean
  // Image
  image_backend?: string | null
  vision_model?: string | null
  // Review
  auto_approve?: boolean
  // Regeneration
  force?: boolean
}

export interface ChannelMeta {
  name: string
  channel_name: string
  niche: string
  language: string
  auth_connected: boolean
  proxy: string
}

export interface ChannelAuthStatus {
  channel: string
  connected: boolean
  token_file: string | null
}

export interface BrandingRequest {
  description?: string | null
  keywords?: string[] | null
  country?: string | null
  banner_path?: string | null
}

export interface BrandingJob {
  job_id: string
  status: 'running' | 'done' | 'failed'
  channel: string
  error: string
}

export interface CompetitorResult {
  description: string
  keywords: string[]
  analysis: string
  competitors_found: number
  competitors_failed: number
}

export interface SecretsStatus {
  exists: boolean
  path: string
  client_id_preview: string
}

export interface PromptMeta {
  name: string
  filename: string
  size_bytes: number
}

export interface VoiceMeta {
  id: string
  name: string
  voice_id: string
  source: string
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (_apiKey) headers['X-API-Key'] = _apiKey
  const res = await fetch(BASE + path, { headers, ...init })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  // 204 No Content — return undefined cast to T
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

/** Build a WebSocket URL with the current api key (for auth). */
export function wsUrl(jobId: string): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const base  = `${proto}://${location.host}/ws/${jobId}`
  return _apiKey ? `${base}?api_key=${encodeURIComponent(_apiKey)}` : base
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

export const api = {
  jobs: {
    list: (limit = 50) => req<Job[]>(`/jobs?limit=${limit}`),
    get: (id: string) => req<Job>(`/jobs/${id}`),
    cancel: (id: string) =>
      req<{ status: string }>(`/jobs/${id}`, { method: 'DELETE' }),
    approve: (id: string, stage: string) =>
      req<{ approved: boolean; stage: string }>(`/jobs/${id}/approve?stage=${stage}`, { method: 'POST' }),
    regenImages: (id: string) =>
      req<{ job_id: string; validation: Record<string, unknown> }>(`/jobs/${id}/regen-images`, { method: 'POST' }),
    regenScript: (id: string) =>
      req<{ job_id: string; word_count: number; block_count: number }>(`/jobs/${id}/regen-script`, { method: 'POST' }),
  },

  pipeline: {
    run: (body: PipelineRunRequest) =>
      req<Job>('/pipeline/run', { method: 'POST', body: JSON.stringify(body) }),
    quick: (body: QuickRunRequest) =>
      req<Job>('/pipeline/quick', { method: 'POST', body: JSON.stringify(body) }),
    quickBatch: (body: QuickBatchRequest) =>
      req<Job[]>('/pipeline/quick-batch', { method: 'POST', body: JSON.stringify(body) }),
  },

  batch: {
    run: (body: BatchRunRequest) =>
      req<Job>('/batch/run', { method: 'POST', body: JSON.stringify(body) }),
    runMulti: (body: MultiBatchRequest) =>
      req<Job[]>('/batch/multi', { method: 'POST', body: JSON.stringify(body) }),
    appendMulti: (body: MultiBatchRequest) =>
      req<Job[]>('/batch/append', { method: 'POST', body: JSON.stringify(body) }),
  },

  videos: {
    list: (params?: { channel?: string; status?: string; limit?: number }) => {
      const q = new URLSearchParams()
      if (params?.channel) q.set('channel', params.channel)
      if (params?.status) q.set('status', params.status)
      if (params?.limit) q.set('limit', String(params.limit))
      return req<Video[]>(`/videos?${q}`)
    },
    get: (id: number) => req<VideoDetail>(`/videos/${id}`),
    costs: (id: number) => req<CostEntry[]>(`/videos/${id}/costs`),
  },

  stats: {
    get: () => req<Stats>('/stats'),
  },

  script: {
    exists: (source_dir: string) =>
      req<{ exists: boolean; path: string }>(`/script/exists?source_dir=${encodeURIComponent(source_dir)}`),
    get: (source_dir: string) =>
      req<Script>(`/script?source_dir=${encodeURIComponent(source_dir)}`),
    save: (source_dir: string, script: Script) =>
      req<{ saved: boolean; path: string }>(`/script?source_dir=${encodeURIComponent(source_dir)}`, {
        method: 'PUT',
        body: JSON.stringify(script),
      }),
  },

  channels: {
    list: () => req<ChannelMeta[]>('/channels'),
    get: (name: string) => req<Record<string, unknown>>(`/channels/${name}`),
    save: (name: string, data: Record<string, unknown>) =>
      req<{ saved: boolean; name: string }>(`/channels/${name}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    delete: (name: string) =>
      req<{ deleted: boolean }>(`/channels/${name}`, { method: 'DELETE' }),
    // OAuth per-channel
    authStatus: (name: string) =>
      req<ChannelAuthStatus>(`/channels/${name}/auth`),
    authConnect: (name: string) =>
      req<{ status: string; message: string }>(`/channels/${name}/auth`, { method: 'POST' }),
    authRevoke: (name: string) =>
      req<{ status: string }>(`/channels/${name}/auth`, { method: 'DELETE' }),
    // Branding
    applyBranding: (name: string, body: BrandingRequest) =>
      req<BrandingJob>(`/channels/${name}/branding`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    brandingStatus: (name: string, jobId: string) =>
      req<BrandingJob>(`/channels/${name}/branding/${jobId}`),
    // Competitor analysis
    analyzeCompetitors: (name: string, urls: string[]) =>
      req<CompetitorResult>(`/channels/${name}/analyze-competitors`, {
        method: 'POST',
        body: JSON.stringify({ urls }),
      }),
    // Shared client_secrets.json
    secretsStatus: () =>
      req<SecretsStatus>('/channels/secrets-status'),
    saveSecrets: (content: string) =>
      req<{ saved: boolean }>('/channels/secrets', {
        method: 'POST',
        body: JSON.stringify({ content }),
      }),
  },

  prompts: {
    list: () => req<PromptMeta[]>('/prompts'),
    get: (name: string) =>
      req<{ name: string; filename: string; content: string }>(`/prompts/${name}`),
    save: (name: string, content: string, filename?: string) =>
      req<{ saved: boolean; name: string }>(`/prompts/${name}`, {
        method: 'PUT',
        body: JSON.stringify({ content, filename }),
      }),
  },

  voices: {
    list: () => req<VoiceMeta[]>('/voices'),
  },

  transcriber: {
    status: () =>
      req<TranscriberStatus>('/transcriber/status'),
    launch: () =>
      req<{ status: string; path: string }>('/transcriber/launch', { method: 'POST' }),
    outputs: (since = 0) =>
      req<TranscriberOutput[]>(`/transcriber/outputs?since=${since}`),
  },

  transcribe: {
    start: (body: TranscribeRequest) =>
      req<{ job_id: string; status: string; url: string }>(
        '/transcribe',
        { method: 'POST', body: JSON.stringify(body) },
      ),
    get: (jobId: string) =>
      req<TranscribeJob>(`/transcribe/${jobId}`),
    list: () =>
      req<TranscribeJob[]>('/transcribe'),
  },

  style: {
    /**
     * Analyze a reference image and return a compact image_style descriptor string.
     * Uses multipart/form-data — do NOT use the req() helper (it sets JSON Content-Type).
     */
    analyze: async (image: File): Promise<{ style: string }> => {
      const form = new FormData()
      form.append('image', image)
      const headers: Record<string, string> = {}
      if (_apiKey) headers['X-API-Key'] = _apiKey
      const res = await fetch(BASE + '/style/analyze', { method: 'POST', body: form, headers })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`${res.status}: ${text.slice(0, 200)}`)
      }
      return res.json() as Promise<{ style: string }>
    },
  },

  music: {
    list: () => req<MusicTrack[]>('/music'),
  },

  projects: {
    /** List subfolders of projects/ with metadata: has_config, video_count */
    folders: () =>
      req<{ name: string; has_config: boolean; video_count: number }[]>('/projects/folders'),
  },

  fs: {
    open: (path: string) =>
      req<{ status: string; path: string }>('/fs/open', {
        method: 'POST',
        body: JSON.stringify({ path }),
      }),
  },

  youtube: {
    status: () =>
      req<{ connected: boolean; token_file?: string; expiry?: string; reason?: string }>('/youtube/status'),
    auth: () =>
      req<{ status: string; message?: string }>('/youtube/auth', { method: 'POST' }),
    revoke: () =>
      req<{ status: string }>('/youtube/auth/revoke', { method: 'POST' }),
    ready: () =>
      req<YoutubeReadyVideo[]>('/youtube/ready'),
    upload: (body: YoutubeUploadRequest) =>
      req<YoutubeUploadJob>('/youtube/upload', { method: 'POST', body: JSON.stringify(body) }),
    uploads: () =>
      req<YoutubeUploadJob[]>('/youtube/uploads'),
    uploadJob: (id: string) =>
      req<YoutubeUploadJob>(`/youtube/uploads/${id}`),
  },

  presets: {
    list: () =>
      req<Preset[]>('/presets'),
    create: (body: PresetCreate) =>
      req<Preset>('/presets', { method: 'POST', body: JSON.stringify(body) }),
    update: (id: string, body: PresetCreate) =>
      req<Preset>(`/presets/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
    delete: (id: string) =>
      req<void>(`/presets/${id}`, { method: 'DELETE' }),
  },

  drive: {
    status: () =>
      req<{ authenticated: boolean; root_folder_id: string }>('/drive/status'),
    auth: () =>
      req<{ status: string; message: string }>('/drive/auth', { method: 'POST' }),
    revoke: () =>
      req<{ status: string; message: string }>('/drive/auth/revoke', { method: 'POST' }),
    upload: (body: { project_dir: string; channel?: string; channel_name?: string; root_folder_id?: string }) =>
      req<{ upload_id: string; status: string }>('/drive/upload', { method: 'POST', body: JSON.stringify(body) }),
    uploadStatus: (id: string) =>
      req<DriveUploadJob>(`/drive/uploads/${id}`),
    uploads: () =>
      req<DriveUploadJob[]>('/drive/uploads'),
    ensureChannelFolders: () =>
      req<{ job_id: string; status: string; channels: string[] }>('/drive/ensure-channel-folders', { method: 'POST' }),
    channels: () =>
      req<DriveChannel[]>('/drive/channels'),
  },

  status: {
    balances: () => req<{
      voiceapi:  { balance_chars?: number; balance_text?: string; error?: string }
      voidai:    { balance_usd?: number | null; note?: string; error?: string }
      wavespeed: { subscription_end?: string | null; note?: string; error?: string }
    }>('/status/balances'),
  },

  auth: {
    status: () =>
      fetch(BASE + '/auth/status').then(r => r.json()) as Promise<{ protected: boolean }>,
    verify: (code: string) =>
      fetch(BASE + '/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      }).then(async r => {
        if (!r.ok) throw new Error('Wrong access code')
        return r.json() as Promise<{ ok: boolean }>
      }),
  },
}

// ── YouTube interfaces ─────────────────────────────────────────────────────────

export interface ThumbnailVariant {
  index: number
  filename: string
  url: string
  size_kb: number
}

export interface YoutubeReadyVideo {
  dir: string
  name: string
  title: string
  title_variants: string[]
  description: string
  tags: string[]
  category_id: string
  language: string
  video_size_mb: number
  has_thumbnail: boolean
  tags_count: number
  thumbnail_variants: ThumbnailVariant[]
  uploaded: YoutubeUploadResult | null
}

export interface YoutubeUploadResult {
  video_id: string
  url: string
  title: string
  privacy: string
  publish_at: string | null
  thumbnail_ok: boolean
  uploaded_at: string
}

export interface YoutubeUploadRequest {
  project_dir: string
  channel?: string
  privacy?: string
  schedule?: string | null
  auto_schedule?: boolean
  dry_run?: boolean
  selected_thumbnail?: string | null
  selected_title?: string | null
}

export interface YoutubeUploadJob {
  job_id: string
  status: string
  project: string
  error: string
  result: YoutubeUploadResult | null
}

// ── Transcriber interfaces ─────────────────────────────────────────────────────

export interface TranscriberStatus {
  transcriber_found: boolean
  transcriber_path: string
  output_dir: string
  output_dir_exists: boolean
  outputs_count: number
}

export interface TranscriberOutput {
  dir: string
  name: string
  title: string
  language: string
  modified_at: number
  has_srt: boolean
  has_description: boolean
  has_thumbnail: boolean
}

export interface TranscribeRequest {
  url: string
  language?: string | null
  auto_pipeline?: boolean
  channel?: string
  quality?: string
  template?: string
  draft?: boolean
  dry_run?: boolean
  background_music?: boolean
  skip_thumbnail?: boolean
  image_style?: string | null
  voice_id?: string | null
  master_prompt?: string | null
  duration_min?: number | null
  duration_max?: number | null
  music_volume?: number | null
  custom_topic?: string | null
}

export interface TranscribeJob {
  job_id: string
  url: string
  status: string   // queued | running | done | failed
  logs: string[]
  error: string
  out_dir: string
}

// ── Google Drive interfaces ────────────────────────────────────────────────────

export interface DriveChannel {
  id: string           // "config/channels/history.json"
  file: string
  channel_name: string // "History Explained"
  niche: string
  language: string
}

export interface DriveUploadJob {
  upload_id: string
  status: string   // running | done | failed
  project_dir: string
  channel_name: string
  folder_url: string | null
  uploaded_files: string[]
  error: string | null
}

// ── Presets interfaces ─────────────────────────────────────────────────────────

export interface Preset {
  id: string
  name: string
  channel: string
  quality: string
  duration_min: number
  duration_max: number
  template: string
  parallel: number
  skip_thumbnail: boolean
  auto_approve: boolean
  image_backend: string
  background_music: boolean
  burn_subtitles: boolean
  no_ken_burns: boolean
  master_prompt: string | null
  image_style: string
  voice_id: string
  music_volume: number | null
  vision_model: string
}

export type PresetCreate = Omit<Preset, 'id'>
