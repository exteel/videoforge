/**
 * YoutubePanel — Upload completed videos to YouTube.
 *
 * Safety notes (щоб не заблокували канал):
 * - Uploading as "private" + schedule є найбезпечнішим способом
 * - Auto-schedule розподіляє відео рівномірно (interval_days з channel config)
 * - Мінімум 1 відео / день; не завантажуй 10 одразу
 */

import { useEffect, useState } from 'react'
import {
  api,
  type YoutubeReadyVideo,
  type YoutubeUploadJob,
  type ChannelMeta,
} from '../api'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString('uk-UA', { dateStyle: 'short', timeStyle: 'short' }) }
  catch { return iso }
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    queued:  'bg-yellow-900 text-yellow-300',
    running: 'bg-blue-900 text-blue-300',
    done:    'bg-green-900 text-green-300',
    failed:  'bg-red-900 text-red-300',
  }
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${colors[status] ?? 'bg-gray-700 text-gray-300'}`}>
      {status}
    </span>
  )
}

// ── Upload row ────────────────────────────────────────────────────────────────

interface RowState {
  privacy: string
  schedule: string
  auto_schedule: boolean
  channel: string
  dry_run: boolean
}

function VideoRow({
  video,
  channels,
  onUploaded,
}: {
  video: YoutubeReadyVideo
  channels: ChannelMeta[]
  onUploaded: () => void
}) {
  const [state, setState] = useState<RowState>({
    privacy:       'private',
    schedule:      '',
    auto_schedule: false,
    channel:       channels[0] ? `config/channels/${channels[0].name}.json` : '',
    dry_run:       false,
  })
  const [uploading, setUploading] = useState(false)
  const [job, setJob] = useState<YoutubeUploadJob | null>(null)
  const [err, setErr] = useState('')

  // Poll active job
  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'failed') return
    const id = setInterval(async () => {
      try {
        const updated = await api.youtube.uploadJob(job.job_id)
        setJob(updated)
        if (updated.status === 'done' || updated.status === 'failed') {
          clearInterval(id)
          onUploaded()
        }
      } catch { /* ignore */ }
    }, 2000)
    return () => clearInterval(id)
  }, [job, onUploaded])

  async function handleUpload() {
    setErr('')
    setUploading(true)
    try {
      const j = await api.youtube.upload({
        project_dir:   video.dir,
        channel:       state.channel,
        privacy:       state.privacy,
        schedule:      state.schedule || null,
        auto_schedule: state.auto_schedule,
        dry_run:       state.dry_run,
      })
      setJob(j)
    } catch (e) {
      setErr(String(e))
    } finally {
      setUploading(false)
    }
  }

  const alreadyUploaded = !!video.uploaded && !state.dry_run

  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div className="min-w-0">
          <div className="text-sm font-medium text-white truncate">{video.title}</div>
          <div className="text-xs text-gray-400 mt-0.5">
            {video.video_size_mb} MB
            {video.has_thumbnail && ' · 🖼 thumbnail'}
            {video.tags_count > 0 && ` · ${video.tags_count} tags`}
            {video.language && ` · ${video.language}`}
          </div>
        </div>
        {alreadyUploaded && (
          <a
            href={video.uploaded!.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-green-400 hover:underline shrink-0"
          >
            ✓ YouTube ↗
          </a>
        )}
      </div>

      {/* Already uploaded summary */}
      {alreadyUploaded && (
        <div className="text-xs text-gray-400 bg-gray-900 rounded p-2">
          Завантажено {fmtDate(video.uploaded!.uploaded_at)} · {video.uploaded!.privacy}
          {video.uploaded!.publish_at && ` · заплановано ${fmtDate(video.uploaded!.publish_at)}`}
        </div>
      )}

      {/* Upload form */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {/* Channel */}
        <label className="space-y-1">
          <span className="text-xs text-gray-400">Канал</span>
          <select
            value={state.channel}
            onChange={(e) => setState((s) => ({ ...s, channel: e.target.value }))}
            className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-blue-500"
          >
            {channels.map((c) => (
              <option key={c.name} value={`config/channels/${c.name}.json`}>
                {c.channel_name}
              </option>
            ))}
          </select>
        </label>

        {/* Privacy */}
        <label className="space-y-1">
          <span className="text-xs text-gray-400">Приватність</span>
          <select
            value={state.privacy}
            onChange={(e) => setState((s) => ({ ...s, privacy: e.target.value }))}
            className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-blue-500"
          >
            <option value="private">Private (безпечно)</option>
            <option value="unlisted">Unlisted</option>
            <option value="public">Public (одразу)</option>
          </select>
        </label>

        {/* Schedule */}
        <label className="space-y-1">
          <span className="text-xs text-gray-400">Планування</span>
          <input
            type="datetime-local"
            value={state.schedule}
            onChange={(e) => setState((s) => ({ ...s, schedule: e.target.value, auto_schedule: false }))}
            disabled={state.auto_schedule}
            className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-blue-500 disabled:opacity-40"
          />
        </label>

        {/* Checkboxes */}
        <div className="flex flex-col justify-end gap-1.5 pb-0.5">
          <label className="flex items-center gap-2 cursor-pointer text-xs text-gray-300">
            <input
              type="checkbox"
              checked={state.auto_schedule}
              onChange={(e) => setState((s) => ({ ...s, auto_schedule: e.target.checked, schedule: '' }))}
              className="accent-blue-500"
            />
            Auto-schedule
          </label>
          <label className="flex items-center gap-2 cursor-pointer text-xs text-gray-300">
            <input
              type="checkbox"
              checked={state.dry_run}
              onChange={(e) => setState((s) => ({ ...s, dry_run: e.target.checked }))}
              className="accent-blue-500"
            />
            Dry run
          </label>
        </div>
      </div>

      {/* Job status */}
      {job && (
        <div className="flex items-center gap-3 text-xs">
          <StatusBadge status={job.status} />
          {job.error && <span className="text-red-300">{job.error}</span>}
          {job.result && (
            <a
              href={job.result.url}
              target="_blank"
              rel="noreferrer"
              className="text-green-400 hover:underline"
            >
              {job.result.url} ({job.result.privacy})
              {job.result.publish_at && ` → ${fmtDate(job.result.publish_at)}`}
            </a>
          )}
        </div>
      )}

      {err && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{err}</div>}

      <button
        onClick={handleUpload}
        disabled={uploading || (!!job && job.status === 'running')}
        className="bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white text-xs font-medium px-4 py-1.5 rounded transition-colors"
      >
        {uploading || job?.status === 'running'
          ? '⏳ Завантаження…'
          : alreadyUploaded
          ? '🔄 Завантажити ще раз'
          : '▲ Upload to YouTube'}
      </button>
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────

export function YoutubePanel() {
  const [authStatus, setAuthStatus] = useState<{ connected: boolean; expiry?: string } | null>(null)
  const [authLoading, setAuthLoading] = useState(false)
  const [videos, setVideos] = useState<YoutubeReadyVideo[]>([])
  const [channels, setChannels] = useState<ChannelMeta[]>([])
  const [loading, setLoading] = useState(true)
  const [authMsg, setAuthMsg] = useState('')
  const [envWarning, setEnvWarning] = useState(false)

  async function loadAll() {
    try {
      const [status, vids, chans] = await Promise.all([
        api.youtube.status(),
        api.youtube.ready(),
        api.channels.list(),
      ])
      setAuthStatus(status)
      setVideos(vids)
      setChannels(chans)
    } catch {
      /* ignore */
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAll()
    // Poll auth status every 3s until connected (user doing OAuth flow)
    const t = setInterval(async () => {
      try {
        const s = await api.youtube.status()
        setAuthStatus(s)
      } catch { /* ignore */ }
    }, 3000)
    return () => clearInterval(t)
  }, [])

  async function handleConnect() {
    setAuthLoading(true)
    setAuthMsg('')
    try {
      const res = await api.youtube.auth()
      setAuthMsg(res.message ?? res.status)
      if (res.status === 'already_connected') {
        const s = await api.youtube.status()
        setAuthStatus(s)
      }
    } catch (e) {
      setAuthMsg(String(e))
      setEnvWarning(true)
    } finally {
      setAuthLoading(false)
    }
  }

  async function handleRevoke() {
    await api.youtube.revoke()
    setAuthStatus({ connected: false })
  }

  const notUploaded = videos.filter((v) => !v.uploaded)
  const uploaded    = videos.filter((v) => !!v.uploaded)

  return (
    <div className="space-y-6">
      {/* Auth card */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <div className="text-sm font-semibold text-white">YouTube — OAuth2</div>
            <div className="text-xs text-gray-400 mt-0.5">
              {authStatus === null
                ? 'Перевірка…'
                : authStatus.connected
                ? `✅ Підключено${authStatus.expiry ? ` (токен до ${fmtDate(authStatus.expiry)})` : ''}`
                : '❌ Не підключено'}
            </div>
          </div>
          <div className="flex gap-2">
            {authStatus?.connected
              ? (
                <button
                  onClick={handleRevoke}
                  className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-300"
                >
                  Відключити
                </button>
              )
              : (
                <button
                  onClick={handleConnect}
                  disabled={authLoading}
                  className="text-xs px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white font-medium"
                >
                  {authLoading ? '…' : '🔑 Підключити YouTube'}
                </button>
              )
            }
            <button
              onClick={loadAll}
              className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-300"
            >
              ↻ Оновити
            </button>
          </div>
        </div>

        {authMsg && (
          <div className={`text-xs rounded p-2 ${envWarning ? 'bg-red-950 text-red-300' : 'bg-blue-950 text-blue-300'}`}>
            {authMsg}
          </div>
        )}

        {envWarning && (
          <div className="text-xs bg-yellow-950 text-yellow-300 rounded p-3 space-y-1">
            <div className="font-semibold">Потрібні .env змінні:</div>
            <code className="block">YOUTUBE_CLIENT_ID=your-client-id</code>
            <code className="block">YOUTUBE_CLIENT_SECRET=your-client-secret</code>
            <div className="text-gray-400 mt-1">
              Отримай в Google Cloud Console → APIs &amp; Services → OAuth 2.0 Client IDs.
              Дозволь youtube.upload scope.
            </div>
          </div>
        )}

        {/* Safety tips */}
        <div className="text-xs text-gray-500 bg-gray-900 rounded p-3 space-y-1">
          <div className="font-medium text-gray-400">🛡 Поради безпеки каналу:</div>
          <ul className="list-disc list-inside space-y-0.5 ml-1">
            <li>Завантажуй як <strong>Private + schedule</strong> — YouTube не вважає це спамом</li>
            <li>Використовуй <strong>Auto-schedule</strong> для рівномірного розподілу (interval_days з конфігу)</li>
            <li>Не завантажуй більше 3-5 відео на день на новий канал</li>
            <li>Переконайся що metadata.json (title, description, tags) унікальні для кожного відео</li>
          </ul>
        </div>
      </div>

      {loading && <div className="text-sm text-gray-400 text-center py-8">Завантаження…</div>}

      {/* Not yet uploaded */}
      {!loading && notUploaded.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            Готові до завантаження ({notUploaded.length})
          </h2>
          {notUploaded.map((v) => (
            <VideoRow key={v.dir} video={v} channels={channels} onUploaded={loadAll} />
          ))}
        </div>
      )}

      {/* Already uploaded */}
      {!loading && uploaded.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            Вже завантажені ({uploaded.length})
          </h2>
          {uploaded.map((v) => (
            <VideoRow key={v.dir} video={v} channels={channels} onUploaded={loadAll} />
          ))}
        </div>
      )}

      {!loading && videos.length === 0 && (
        <div className="text-sm text-gray-500 text-center py-8">
          Немає проектів з final.mp4. Спочатку згенеруй відео на вкладці Jobs.
        </div>
      )}
    </div>
  )
}
