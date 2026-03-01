import { useEffect, useState } from 'react'
import { api, type YoutubeReadyVideo, type YoutubeUploadJob, type ThumbnailVariant } from '../api'

// ── Upload form state per project ──────────────────────────────────────────────

interface UploadForm {
  selectedThumbnail: string | null
  selectedTitle:     string
  privacy:           'private' | 'unlisted' | 'public'
  schedule:          string
  autoSchedule:      boolean
  dryRun:            boolean
}

// ── Thumbnail picker ───────────────────────────────────────────────────────────

function ThumbnailPicker({
  variants, selected, onSelect,
}: {
  variants: ThumbnailVariant[]
  selected: string | null
  onSelect: (filename: string) => void
}) {
  if (!variants.length) return <p className="text-xs text-gray-500 italic">Немає превью</p>
  return (
    <div className="flex flex-wrap gap-2">
      {variants.map((v) => (
        <button key={v.filename} type="button" onClick={() => onSelect(v.filename)}
          className={`relative rounded overflow-hidden border-2 transition-all ${
            selected === v.filename
              ? 'border-blue-500 shadow-lg shadow-blue-500/30'
              : 'border-gray-600 hover:border-gray-400'
          }`}
          title={`${v.filename} (${v.size_kb} KB)`}
        >
          <img src={v.url} alt={`Thumbnail ${v.index}`}
            className="w-36 h-20 object-cover"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <span className={`absolute bottom-0 left-0 right-0 text-center text-xs py-0.5 ${
            selected === v.filename ? 'bg-blue-600 text-white' : 'bg-black/60 text-gray-200'
          }`}>#{v.index}</span>
          {selected === v.filename && (
            <span className="absolute top-1 right-1 bg-blue-600 rounded-full w-5 h-5 flex items-center justify-center text-white text-xs font-bold">✓</span>
          )}
        </button>
      ))}
    </div>
  )
}

// ── Title selector ─────────────────────────────────────────────────────────────

function TitleSelector({
  variants, selected, onSelect,
}: {
  variants: string[]
  selected: string
  onSelect: (t: string) => void
}) {
  if (variants.length <= 1) return null
  return (
    <div className="space-y-1.5">
      {variants.map((t, i) => (
        <label key={i}
          className={`flex items-start gap-2 p-2 rounded border cursor-pointer transition-colors ${
            selected === t
              ? 'border-blue-500 bg-blue-500/10'
              : 'border-gray-600 hover:border-gray-500 hover:bg-gray-700/30'
          }`}
        >
          <input type="radio" name="title" checked={selected === t}
            onChange={() => onSelect(t)}
            className="mt-0.5 accent-blue-500 shrink-0"
          />
          <span className="text-sm text-gray-200 leading-snug">
            <span className="text-xs text-gray-500 mr-1.5">#{i + 1}</span>{t}
          </span>
        </label>
      ))}
    </div>
  )
}

// ── Single video row ───────────────────────────────────────────────────────────

function VideoRow({ video, channel, onUploaded }: {
  video: YoutubeReadyVideo
  channel: string
  onUploaded: () => void
}) {
  const initTitle = video.title_variants?.[0] ?? video.title
  const [form, setForm] = useState<UploadForm>({
    selectedThumbnail: video.thumbnail_variants?.[0]?.filename ?? null,
    selectedTitle:     initTitle,
    privacy:           'private',
    schedule:          '',
    autoSchedule:      false,
    dryRun:            false,
  })
  const [job, setJob]       = useState<YoutubeUploadJob | null>(null)
  const [open, setOpen]     = useState(false)
  const [submitting, setSub] = useState(false)
  const [error, setError]   = useState('')

  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'failed') return
    const t = setInterval(async () => {
      try {
        const u = await api.youtube.uploadJob(job.job_id)
        setJob(u)
        if (u.status === 'done') onUploaded()
      } catch { /* ignore */ }
    }, 2000)
    return () => clearInterval(t)
  }, [job, onUploaded])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setSub(true)
    try {
      const j = await api.youtube.upload({
        project_dir:        video.dir,
        channel,
        privacy:            form.privacy,
        schedule:           form.schedule || null,
        auto_schedule:      form.autoSchedule,
        dry_run:            form.dryRun,
        selected_thumbnail: form.selectedThumbnail,
        selected_title:     form.selectedTitle || null,
      })
      setJob(j)
    } catch (err) {
      setError(String(err))
    } finally {
      setSub(false)
    }
  }

  const isUploaded = !!video.uploaded
  const jobColor = !job ? '' :
    job.status === 'done'    ? 'text-green-400' :
    job.status === 'failed'  ? 'text-red-400'   :
    job.status === 'running' ? 'text-yellow-400' : 'text-gray-400'

  return (
    <div className={`bg-gray-800 rounded-lg border overflow-hidden ${isUploaded ? 'border-green-700/40' : 'border-gray-700'}`}>
      {/* Header */}
      <button type="button" onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-gray-700/30 transition-colors"
      >
        <span className="text-sm font-medium text-white flex-1 truncate">{video.title}</span>
        <div className="flex items-center gap-2 text-xs text-gray-500 shrink-0">
          {isUploaded && <span className="text-green-400 font-medium">✓ Завантажено</span>}
          <span>{video.video_size_mb} MB</span>
          {video.thumbnail_variants.length > 0 && (
            <span className="text-blue-400">{video.thumbnail_variants.length} превью</span>
          )}
          {(video.title_variants?.length ?? 0) > 1 && (
            <span className="text-purple-400">{video.title_variants.length} назви</span>
          )}
          <span className="text-gray-600">{open ? '▲' : '▼'}</span>
        </div>
      </button>

      {open && (
        <form onSubmit={submit} className="border-t border-gray-700 p-4 space-y-5">

          {/* Thumbnail A/B picker */}
          {video.thumbnail_variants.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
                🖼 Превью — обери для A/B тесту
              </p>
              <ThumbnailPicker
                variants={video.thumbnail_variants}
                selected={form.selectedThumbnail}
                onSelect={(f) => setForm(v => ({ ...v, selectedThumbnail: f }))}
              />
            </div>
          )}

          {/* Title variants */}
          {(video.title_variants?.length ?? 0) > 1 && (
            <div className="space-y-2">
              <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
                ✏️ Назва — обери варіант
              </p>
              <TitleSelector
                variants={video.title_variants}
                selected={form.selectedTitle}
                onSelect={(t) => setForm(v => ({ ...v, selectedTitle: t }))}
              />
            </div>
          )}

          {/* Description preview */}
          {video.description && (
            <details>
              <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-300 select-none">
                📝 Опис відео (розгорнути)
              </summary>
              <pre className="mt-2 text-xs text-gray-400 bg-gray-900 rounded p-2 whitespace-pre-wrap max-h-32 overflow-y-auto font-sans">
                {video.description}
              </pre>
            </details>
          )}

          {/* Tags */}
          {video.tags.length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1.5">🏷 Теги ({video.tags.length})</p>
              <div className="flex flex-wrap gap-1">
                {video.tags.slice(0, 15).map((t) => (
                  <span key={t} className="text-xs bg-gray-700 text-gray-300 rounded px-1.5 py-0.5">{t}</span>
                ))}
                {video.tags.length > 15 && (
                  <span className="text-xs text-gray-500">+{video.tags.length - 15} ще</span>
                )}
              </div>
            </div>
          )}

          {/* Upload settings */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="space-y-1">
              <span className="text-xs text-gray-400">Видимість</span>
              <select value={form.privacy}
                onChange={(e) => setForm({ ...form, privacy: e.target.value as UploadForm['privacy'] })}
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
              >
                <option value="private">🔒 Закрита (рекомендовано)</option>
                <option value="unlisted">🔗 За посиланням</option>
                <option value="public">🌍 Публічна</option>
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-xs text-gray-400">Запланувати</span>
              <input type="text" placeholder="2026-03-15 18:00"
                value={form.schedule}
                onChange={(e) => setForm({ ...form, schedule: e.target.value })}
                className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
            </label>
          </div>

          <div className="flex flex-wrap gap-4 text-sm text-gray-300">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={form.autoSchedule}
                onChange={(e) => setForm({ ...form, autoSchedule: e.target.checked })}
                className="accent-blue-500" />
              <span>Auto-schedule</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={form.dryRun}
                onChange={(e) => setForm({ ...form, dryRun: e.target.checked })}
                className="accent-blue-500" />
              <span>Dry run</span>
            </label>
          </div>

          {error && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{error}</div>}

          {job && (
            <div className={`text-xs rounded p-2 bg-gray-900 ${jobColor}`}>
              {job.status === 'running' && '⏳ Завантажується…'}
              {job.status === 'queued'  && '⏸ В черзі…'}
              {job.status === 'done' && job.result && (
                <>✅ Готово! <a href={job.result.url} target="_blank" rel="noreferrer"
                  className="underline text-blue-400 ml-1">{job.result.url}</a></>
              )}
              {job.status === 'failed' && `❌ Помилка: ${job.error}`}
            </div>
          )}

          <button type="submit"
            disabled={submitting || job?.status === 'running'}
            className="bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm font-semibold px-5 py-2 rounded transition-colors"
          >
            {submitting || job?.status === 'running' ? '⏳ Завантажується…' : '▲ Завантажити на YouTube'}
          </button>
        </form>
      )}
    </div>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export function YoutubePanel() {
  const [videos, setVideos]          = useState<YoutubeReadyVideo[]>([])
  const [auth, setAuth]              = useState<{ connected: boolean; expiry?: string } | null>(null)
  const [authLoading, setAuthLoading] = useState(false)
  const [loading, setLoading]        = useState(true)
  const [channel, setChannel]        = useState('config/channels/history.json')

  async function loadAll() {
    try {
      const [vids, status] = await Promise.all([
        api.youtube.ready(),
        api.youtube.status(),
      ])
      setVideos(vids)
      setAuth(status as { connected: boolean; expiry?: string })
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => {
    loadAll()
    const t = setInterval(loadAll, 10_000)
    return () => clearInterval(t)
  }, [])

  async function startAuth() {
    setAuthLoading(true)
    try {
      await api.youtube.auth()
      let tries = 0
      const poll = setInterval(async () => {
        const s = await api.youtube.status()
        if ((s as { connected: boolean }).connected || ++tries > 30) {
          clearInterval(poll)
          setAuth(s as { connected: boolean; expiry?: string })
          setAuthLoading(false)
        }
      }, 2000)
    } catch { setAuthLoading(false) }
  }

  async function revoke() {
    await api.youtube.revoke()
    setAuth({ connected: false })
  }

  const ready    = videos.filter(v => !v.uploaded)
  const uploaded = videos.filter(v => !!v.uploaded)

  return (
    <div className="space-y-6">

      {/* Auth block */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <p className="text-sm font-medium text-white">YouTube авторизація</p>
            <p className="text-xs text-gray-400 mt-0.5">
              {auth === null ? 'Перевіряється…'
                : auth.connected
                  ? `✅ Підключено${auth.expiry ? ` (до ${auth.expiry.slice(0, 10)})` : ''}`
                  : '❌ Не підключено'}
            </p>
          </div>
          <div className="flex gap-2">
            {!auth?.connected ? (
              <button onClick={startAuth} disabled={authLoading}
                className="bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm px-3 py-1.5 rounded transition-colors">
                {authLoading ? 'Відкриваю браузер…' : 'Підключити Google'}
              </button>
            ) : (
              <button onClick={revoke}
                className="bg-gray-700 hover:bg-gray-600 text-gray-300 text-sm px-3 py-1.5 rounded transition-colors">
                Відключити
              </button>
            )}
            <button onClick={loadAll}
              className="bg-gray-700 hover:bg-gray-600 text-gray-300 text-sm px-3 py-1.5 rounded transition-colors">
              ↺
            </button>
          </div>
        </div>
        <label className="space-y-1 block">
          <span className="text-xs text-gray-400">Channel config</span>
          <input value={channel} onChange={e => setChannel(e.target.value)}
            className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </label>
      </div>

      {/* Safety tips */}
      <div className="bg-yellow-950/40 border border-yellow-700/40 rounded-lg p-3 text-xs text-yellow-200/80 space-y-1">
        <p className="font-semibold text-yellow-300">💡 Рекомендації</p>
        <ul className="list-disc list-inside space-y-0.5 text-yellow-200/70">
          <li>Завжди завантажуй як <strong>Закрита</strong> — перевіряй перед публікацією</li>
          <li>Обери найкраще превью з {ready[0]?.thumbnail_variants?.length ?? 3} варіантів для максимального CTR</li>
          <li>Dry run — перевірить налаштування без реального завантаження</li>
        </ul>
      </div>

      {/* Ready */}
      {loading ? (
        <p className="text-gray-500 text-sm text-center py-8">Завантаження…</p>
      ) : ready.length > 0 ? (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            Готові до завантаження ({ready.length})
          </h2>
          {ready.map(v => (
            <VideoRow key={v.name} video={v} channel={channel} onUploaded={loadAll} />
          ))}
        </div>
      ) : !loading && (
        <div className="text-center py-10 text-gray-500 text-sm">
          <p>Немає відео готових до завантаження.</p>
          <p className="mt-1 text-xs">Запусти пайплайн до кроку 7 (Metadata) щоб з'явились відео.</p>
        </div>
      )}

      {/* Uploaded */}
      {uploaded.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
            Завантажені ({uploaded.length})
          </h2>
          {uploaded.map(v => (
            <div key={v.name}
              className="bg-gray-800/50 rounded-lg border border-green-700/30 px-4 py-3 flex items-center justify-between gap-3">
              <span className="text-sm text-gray-300 truncate">{v.title}</span>
              <div className="flex items-center gap-2 text-xs shrink-0">
                <span className="text-green-400">✓ {v.uploaded!.privacy}</span>
                {v.uploaded!.url && (
                  <a href={v.uploaded!.url} target="_blank" rel="noreferrer"
                    className="text-blue-400 hover:underline">↗ YouTube</a>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
