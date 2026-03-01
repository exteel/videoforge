import { useEffect, useState } from 'react'
import { type Video, type VideoDetail, api } from '../api'

const STATUS_ICON: Record<string, string> = {
  done: '✓',
  failed: '✗',
  running: '~',
  pending: '·',
  skipped: '-',
}

const STATUS_COLOR: Record<string, string> = {
  done:    'text-green-400',
  failed:  'text-red-400',
  running: 'text-blue-400',
  pending: 'text-yellow-400',
  skipped: 'text-gray-500',
}

function fmt(sec: number | null) {
  if (sec == null) return '-'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

function VideoDetailPane({ id, onClose }: { id: number; onClose: () => void }) {
  const [detail, setDetail] = useState<VideoDetail | null>(null)

  useEffect(() => {
    api.videos.get(id).then(setDetail).catch(console.error)
  }, [id])

  if (!detail) return <div className="p-4 text-gray-400 text-sm">Loading…</div>

  const v = detail.video as {
    status?: string; source_title?: string | null; channel?: string
    quality_preset?: string; elapsed_seconds?: number | null
    youtube_url?: string | null; error_message?: string | null
  }

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-lg border border-gray-600 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b border-gray-700">
          <h2 className="text-white font-semibold text-sm">
            Video #{id} — {(v.status ?? '').toUpperCase()}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-lg leading-none">×</button>
        </div>
        <div className="p-4 space-y-4">
          {/* Meta */}
          <div className="text-sm space-y-1">
            {v.source_title && <div><span className="text-gray-400">Title: </span><span className="text-white">{v.source_title}</span></div>}
            <div><span className="text-gray-400">Channel: </span><span className="text-white">{v.channel ?? '-'}</span></div>
            <div><span className="text-gray-400">Preset: </span><span className="text-white">{v.quality_preset ?? '-'}</span></div>
            <div><span className="text-gray-400">Elapsed: </span><span className="text-white">{fmt(v.elapsed_seconds ?? null)}</span></div>
            {v.youtube_url && (
              <div>
                <span className="text-gray-400">YouTube: </span>
                <a href={v.youtube_url} target="_blank" rel="noreferrer" className="text-blue-400 hover:underline text-xs break-all">{v.youtube_url}</a>
              </div>
            )}
            {v.error_message && (
              <div className="text-red-300 bg-red-950 rounded p-2 font-mono text-xs">{v.error_message}</div>
            )}
          </div>

          {/* Cost breakdown */}
          {detail.costs.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">Cost breakdown</h3>
              <table className="w-full text-xs text-left">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-700">
                    <th className="pb-1 pr-3">Step</th>
                    <th className="pb-1 pr-3">Model</th>
                    <th className="pb-1 text-right">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.costs.map((c, i) => (
                    <tr key={i} className="border-b border-gray-700/50">
                      <td className="py-1 pr-3 text-gray-300">{c.step}</td>
                      <td className="py-1 pr-3 text-gray-400 font-mono">{c.model}</td>
                      <td className="py-1 text-right text-green-400">${c.cost_usd.toFixed(4)}</td>
                    </tr>
                  ))}
                  <tr className="font-semibold">
                    <td colSpan={2} className="pt-2 text-gray-300">Total</td>
                    <td className="pt-2 text-right text-green-300">${detail.total_cost_usd.toFixed(4)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export function VideoList() {
  const [videos, setVideos] = useState<Video[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)

  async function load() {
    try {
      const data = await api.videos.list({ status: statusFilter || undefined, limit: 100 })
      setVideos(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [statusFilter])

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex gap-2 flex-wrap">
        {['', 'done', 'failed', 'running', 'pending'].map((s) => (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              statusFilter === s ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
            }`}
          >
            {s || 'All'}
          </button>
        ))}
      </div>

      {/* Table */}
      {loading ? (
        <p className="text-gray-500 text-sm">Loading…</p>
      ) : videos.length === 0 ? (
        <p className="text-gray-500 text-sm text-center py-8">No videos in the database yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead>
              <tr className="text-xs text-gray-500 border-b border-gray-700">
                <th className="pb-2 pr-4">ID</th>
                <th className="pb-2 pr-4">Status</th>
                <th className="pb-2 pr-4">Title / Source</th>
                <th className="pb-2 pr-4">Preset</th>
                <th className="pb-2 pr-4">Elapsed</th>
                <th className="pb-2 pr-4">YouTube</th>
                <th className="pb-2">Date</th>
              </tr>
            </thead>
            <tbody>
              {videos.map((v) => (
                <tr
                  key={v.id}
                  onClick={() => setSelectedId(v.id)}
                  className="border-b border-gray-700/50 hover:bg-gray-700/40 cursor-pointer transition-colors"
                >
                  <td className="py-2 pr-4 text-gray-400 font-mono">#{v.id}</td>
                  <td className={`py-2 pr-4 font-semibold ${STATUS_COLOR[v.status] ?? 'text-gray-400'}`}>
                    {STATUS_ICON[v.status] ?? '?'} {v.status}
                  </td>
                  <td className="py-2 pr-4 text-white max-w-xs truncate">
                    {v.source_title ?? v.source_dir.split('/').pop() ?? v.source_dir}
                  </td>
                  <td className="py-2 pr-4 text-gray-400">{v.quality_preset}</td>
                  <td className="py-2 pr-4 text-gray-400">{fmt(v.elapsed_seconds)}</td>
                  <td className="py-2 pr-4">
                    {v.youtube_url
                      ? <a href={v.youtube_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="text-blue-400 hover:underline">↗</a>
                      : <span className="text-gray-600">-</span>
                    }
                  </td>
                  <td className="py-2 text-gray-500 text-xs">{v.created_at.slice(0, 10)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId != null && (
        <VideoDetailPane id={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </div>
  )
}
