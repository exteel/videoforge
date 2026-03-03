/** VideoForge API client — thin wrapper around fetch */

const BASE = '/api'

export interface Job {
  job_id: string
  kind: 'pipeline' | 'batch'
  status: 'queued' | 'running' | 'waiting_review' | 'done' | 'failed' | 'cancelled'
  source: string
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
  image_style?: string | null
  voice_id?: string | null
  master_prompt?: string | null
  duration_min?: number | null
  duration_max?: number | null
  music_volume?: number | null
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

export interface ChannelMeta {
  name: string
  channel_name: string
  niche: string
  language: string
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
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  return res.json() as Promise<T>
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
  },

  pipeline: {
    run: (body: PipelineRunRequest) =>
      req<Job>('/pipeline/run', { method: 'POST', body: JSON.stringify(body) }),
  },

  batch: {
    run: (body: BatchRunRequest) =>
      req<Job>('/batch/run', { method: 'POST', body: JSON.stringify(body) }),
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
      const res = await fetch(BASE + '/style/analyze', { method: 'POST', body: form })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`${res.status}: ${text.slice(0, 200)}`)
      }
      return res.json() as Promise<{ style: string }>
    },
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
}

export interface TranscribeJob {
  job_id: string
  url: string
  status: string   // queued | running | done | failed
  logs: string[]
  error: string
  out_dir: string
}
