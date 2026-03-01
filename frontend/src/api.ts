/** VideoForge API client — thin wrapper around fetch */

const BASE = '/api'

export interface Job {
  job_id: string
  kind: 'pipeline' | 'batch'
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  source: string
  channel: string
  quality: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  elapsed: number | null
  step: number
  step_name: string
  error: string
  logs: string[]
  db_video_id: number | null
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

export interface PipelineRunRequest {
  source_dir: string
  channel?: string
  quality?: string
  template?: string
  draft?: boolean
  from_step?: number
  budget?: number | null
  langs?: string[] | null
  dry_run?: boolean
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
}
