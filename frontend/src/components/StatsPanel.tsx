import { useEffect, useState } from 'react'
import { type Stats, api } from '../api'

function fmt(sec: number | null) {
  if (sec == null) return '-'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
      <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">{label}</div>
      <div className="text-2xl font-bold text-white">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  )
}

export function StatsPanel() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.stats
      .get()
      .then(setStats)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <p className="text-gray-500 text-sm">Loading…</p>
  if (error) return <p className="text-red-400 text-sm">{error}</p>
  if (!stats) return null

  const successRate =
    stats.total_videos > 0
      ? Math.round((stats.done / stats.total_videos) * 100)
      : 0

  return (
    <div className="space-y-6">
      {/* Key metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total videos" value={stats.total_videos} />
        <StatCard label="Done" value={stats.done} sub={`${successRate}% success`} />
        <StatCard label="Failed" value={stats.failed} />
        <StatCard label="Total cost" value={`$${stats.cost_total_usd.toFixed(4)}`} sub={stats.avg_elapsed != null ? `avg ${fmt(stats.avg_elapsed)}` : undefined} />
      </div>

      {/* By model */}
      {stats.by_model.length > 0 && (
        <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">Cost by model</h3>
          <table className="w-full text-sm text-left">
            <thead>
              <tr className="text-xs text-gray-500 border-b border-gray-700">
                <th className="pb-2 pr-4">Model</th>
                <th className="pb-2 pr-4 text-right">Calls</th>
                <th className="pb-2 text-right">Total</th>
              </tr>
            </thead>
            <tbody>
              {stats.by_model.map((m) => (
                <tr key={m.model} className="border-b border-gray-700/50">
                  <td className="py-1.5 pr-4 font-mono text-gray-300 text-xs">{m.model}</td>
                  <td className="py-1.5 pr-4 text-right text-gray-400">{m.calls}</td>
                  <td className="py-1.5 text-right text-green-400">${m.total.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* By preset */}
      {stats.by_preset.length > 0 && (
        <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">Runs by preset</h3>
          <div className="space-y-2">
            {stats.by_preset.map((p) => {
              const pct = p.total > 0 ? Math.round(((p.done ?? 0) / p.total) * 100) : 0
              return (
                <div key={p.quality_preset} className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-400">
                    <span className="font-medium">{p.quality_preset}</span>
                    <span>{p.done ?? 0} / {p.total} done ({pct}%)</span>
                  </div>
                  <div className="w-full bg-gray-700 rounded-full h-1.5">
                    <div className="bg-green-500 h-1.5 rounded-full" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
