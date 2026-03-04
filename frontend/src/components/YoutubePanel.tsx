import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type BrandingJob,
  type BrandingRequest,
  type ChannelMeta,
  type CompetitorResult,
  type SecretsStatus,
  type YoutubeReadyVideo,
  type YoutubeUploadJob,
  type ThumbnailVariant,
} from '../api'

// ─── Helpers ──────────────────────────────────────────────────────────────────

function Dot({ on }: { on: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${on ? 'bg-green-400' : 'bg-gray-500'}`} />
  )
}

function Tip({ text }: { text: string }) {
  return (
    <span title={text} className="text-gray-500 cursor-help text-xs">ⓘ</span>
  )
}

// ─── Channel list (left sidebar) ──────────────────────────────────────────────

function ChannelSidebar({
  channels, selected, onSelect, onRefresh,
}: {
  channels: ChannelMeta[]
  selected: string | null
  onSelect: (name: string) => void
  onRefresh: () => void
}) {
  return (
    <div className="w-52 shrink-0 border-r border-gray-700 flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700">
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Канали</span>
        <button onClick={onRefresh} className="text-gray-500 hover:text-white text-xs" title="Refresh">↺</button>
      </div>
      <div className="flex-1 overflow-y-auto">
        {channels.length === 0 && (
          <p className="text-xs text-gray-500 p-3 italic">
            Немає каналів.<br />Додайте config у config/channels/
          </p>
        )}
        {channels.map((ch) => (
          <button
            key={ch.name}
            onClick={() => onSelect(ch.name)}
            className={`w-full text-left px-3 py-2.5 flex items-center gap-2 hover:bg-gray-700/50 transition-colors ${
              selected === ch.name ? 'bg-gray-700 border-l-2 border-blue-500' : 'border-l-2 border-transparent'
            }`}
          >
            <Dot on={ch.auth_connected} />
            <div className="min-w-0">
              <div className="text-sm text-white truncate">{ch.channel_name || ch.name}</div>
              <div className="text-xs text-gray-500 truncate">
                {ch.auth_connected ? 'Підключено' : 'Не підключено'}
                {ch.proxy ? ' · proxy' : ''}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

// ─── Client Secrets setup (shared, shown once) ────────────────────────────────

function SecretsSetup() {
  const [status,  setStatus]  = useState<SecretsStatus | null>(null)
  const [raw,     setRaw]     = useState('')
  const [saving,  setSaving]  = useState(false)
  const [msg,     setMsg]     = useState('')
  const [open,    setOpen]    = useState(false)

  useEffect(() => {
    api.channels.secretsStatus().then(setStatus).catch(() => {})
  }, [])

  async function save() {
    setSaving(true); setMsg('')
    try {
      await api.channels.saveSecrets(raw.trim())
      const s = await api.channels.secretsStatus()
      setStatus(s)
      setMsg('✅ client_secrets.json збережено!')
      setOpen(false)
    } catch (e) { setMsg(String(e)) }
    finally { setSaving(false) }
  }

  if (!status) return null

  if (status.exists) {
    return (
      <div className="flex items-center gap-2 text-xs text-green-400 bg-green-900/20 border border-green-800/40 rounded px-3 py-2">
        ✅ client_secrets.json налаштовано
        <span className="text-gray-500 ml-1">({status.client_id_preview})</span>
        <button onClick={() => setOpen(v => !v)} className="ml-auto text-gray-500 hover:text-white">змінити</button>
      </div>
    )
  }

  return (
    <div className="bg-yellow-900/20 border border-yellow-700/40 rounded p-3 space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-yellow-400 text-sm font-medium">⚠️ client_secrets.json не знайдено</span>
        <button onClick={() => setOpen(v => !v)} className="ml-auto text-xs text-blue-400 hover:underline">
          {open ? 'Закрити' : 'Налаштувати'}
        </button>
      </div>

      {open && (
        <div className="space-y-3">
          <div className="text-xs text-gray-300 space-y-1.5 bg-gray-900 rounded p-3">
            <p className="font-semibold text-white">Як отримати client_secrets.json:</p>
            <p>1. Відкрий <a href="https://console.cloud.google.com" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">console.cloud.google.com</a></p>
            <p>2. Створи проект (або обери існуючий)</p>
            <p>3. <strong>APIs & Services → Enable APIs</strong> → знайди <em>YouTube Data API v3</em> → Enable</p>
            <p>4. <strong>APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID</strong></p>
            <p>5. Application type: <strong>Desktop app</strong> → назва: VideoForge → Create</p>
            <p>6. Натисни <strong>Download JSON</strong> → відкрий файл у текстовому редакторі → скопіюй весь вміст нижче</p>
            <p className="text-yellow-400">⚠️ OAuth Consent Screen: додай свій Gmail у "Test users" (або перейди в Production)</p>
          </div>
          <textarea
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            rows={6}
            placeholder={'{\n  "installed": {\n    "client_id": "...",\n    ...\n  }\n}'}
            className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-xs font-mono text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
          />
          <button
            onClick={save}
            disabled={saving || !raw.trim()}
            className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm px-4 py-2 rounded"
          >
            {saving ? 'Зберігаємо…' : 'Зберегти client_secrets.json'}
          </button>
          {msg && <p className={`text-xs ${msg.startsWith('✅') ? 'text-green-400' : 'text-red-400'}`}>{msg}</p>}
        </div>
      )}
    </div>
  )
}

// ─── Auth section ─────────────────────────────────────────────────────────────

function AuthSection({ channelName, channelData, onSaved }: {
  channelName: string
  channelData: Record<string, unknown>
  onSaved: () => void
}) {
  const [connected, setConnected] = useState<boolean | null>(null)
  const [loading, setLoading]     = useState(false)
  const [msg, setMsg]             = useState('')
  const [proxy, setProxy]         = useState((channelData.proxy as string) || '')
  const [savingProxy, setSavingProxy] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await api.channels.authStatus(channelName)
      setConnected(s.connected)
    } catch { /* ignore */ }
  }, [channelName])

  useEffect(() => { load() }, [load])

  async function connect() {
    setLoading(true); setMsg('')
    try {
      const r = await api.channels.authConnect(channelName)
      setMsg(r.message || 'Browser opened — complete OAuth flow then click Refresh')
      const iv = setInterval(async () => {
        const s = await api.channels.authStatus(channelName)
        if (s.connected) {
          setConnected(true)
          setMsg('✅ Підключено!')
          clearInterval(iv)
          onSaved()
        }
      }, 3000)
      setTimeout(() => clearInterval(iv), 120_000)
    } catch (e) { setMsg(String(e)) }
    finally { setLoading(false) }
  }

  async function revoke() {
    if (!confirm('Відключити канал? Токен буде видалено.')) return
    await api.channels.authRevoke(channelName)
    setConnected(false)
    setMsg('Відключено')
    onSaved()
  }

  async function saveProxy() {
    setSavingProxy(true)
    try {
      const updated = { ...channelData, proxy: proxy.trim() || undefined }
      if (!proxy.trim()) delete (updated as Record<string,unknown>).proxy
      await api.channels.save(channelName, updated)
      onSaved()
      setMsg('Proxy збережено')
    } catch (e) { setMsg(String(e)) }
    finally { setSavingProxy(false) }
  }

  return (
    <div className="space-y-5">
      {/* Step 1: Google Cloud credentials */}
      <div className="space-y-1.5">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Крок 1 — Google Cloud (один раз для всіх каналів)</p>
        <SecretsSetup />
      </div>

      <div className="border-t border-gray-700" />

      {/* Step 2: Per-channel OAuth */}
      <div className="space-y-4">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Крок 2 — Авторизація каналу</p>

        {/* Status */}
        <div className="flex items-center gap-3">
          <Dot on={!!connected} />
          <span className="text-sm text-gray-200">
            {connected === null ? 'Перевірка…' : connected ? 'OAuth2 підключено' : 'Не підключено'}
          </span>
          <button onClick={load} className="text-xs text-gray-500 hover:text-white ml-auto">↺ Refresh</button>
        </div>

        {/* Proxy */}
        <div className="space-y-1">
          <label className="text-xs text-gray-400 flex items-center gap-1.5">
            Proxy URL <Tip text="socks5://user:pass@host:port або http://..." />
          </label>
          <div className="flex gap-2">
            <input
              value={proxy}
              onChange={(e) => setProxy(e.target.value)}
              placeholder="socks5://user:pass@host:1080"
              className="flex-1 bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-blue-500"
            />
            <button
              onClick={saveProxy}
              disabled={savingProxy}
              className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-xs px-3 py-1.5 rounded"
            >
              {savingProxy ? '…' : 'Save'}
            </button>
          </div>
          <p className="text-xs text-gray-500">Proxy для API-запитів (не для OAuth browser flow)</p>
        </div>

        {/* Auth buttons */}
        <div className="flex gap-2">
          {!connected ? (
            <button
              onClick={connect}
              disabled={loading}
              className="bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
            >
              {loading ? '⏳ Відкриваємо браузер…' : '🔗 Підключити YouTube'}
            </button>
          ) : (
            <button onClick={revoke} className="bg-gray-700 hover:bg-gray-600 text-white text-sm px-4 py-2 rounded">
              Відключити
            </button>
          )}
        </div>

        {msg && (
          <p className={`text-xs ${msg.startsWith('✅') ? 'text-green-400' : 'text-blue-300'}`}>{msg}</p>
        )}

        <div className="text-xs text-gray-500 bg-gray-900 rounded p-3 space-y-1">
          <p>💡 <strong>Кожен Google акаунт</strong> потребує окремої авторизації. Перед кліком переконайся, що в браузері активний потрібний акаунт.</p>
          <p>💡 <strong>2 канали на 1 акаунт:</strong> обидва канали мають один OAuth токен — достатньо підключити один раз.</p>
        </div>
      </div>
    </div>
  )
}

// ─── Branding section ─────────────────────────────────────────────────────────

function BrandingSection({ channelName, channelData }: {
  channelName: string
  channelData: Record<string, unknown>
}) {
  const branding = (channelData.branding as Record<string, unknown>) || {}

  const [description, setDescription] = useState((branding.description as string) || '')
  const [keywords,    setKeywords]    = useState(
    Array.isArray(branding.keywords) ? (branding.keywords as string[]).join(', ') : ''
  )
  const [country,    setCountry]    = useState((branding.country as string) || 'UA')
  const [bannerPath, setBannerPath] = useState((branding.banner_path as string) || '')

  const [job,     setJob]     = useState<BrandingJob | null>(null)
  const [loading, setLoading] = useState(false)
  const [msg,     setMsg]     = useState('')

  // Competitor analysis
  const [competitorUrls, setCompetitorUrls] = useState('')
  const [analyzing,      setAnalyzing]      = useState(false)
  const [analysisResult, setAnalysisResult] = useState<CompetitorResult | null>(null)
  const [analysisMsg,    setAnalysisMsg]    = useState('')

  // Poll branding job
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const iv = setInterval(async () => {
      const updated = await api.channels.brandingStatus(channelName, job.job_id)
      setJob(updated)
      if (updated.status !== 'running') clearInterval(iv)
    }, 2000)
    return () => clearInterval(iv)
  }, [job, channelName])

  async function analyze() {
    setAnalyzing(true); setAnalysisMsg(''); setAnalysisResult(null)
    try {
      const urls = competitorUrls
        .split('\n')
        .map((u) => u.trim())
        .filter(Boolean)
      if (!urls.length) {
        setAnalysisMsg('Введіть хоча б одне посилання')
        return
      }
      const r = await api.channels.analyzeCompetitors(channelName, urls)
      setAnalysisResult(r)
      if (r.competitors_failed > 0) {
        setAnalysisMsg(`⚠️ ${r.competitors_found} знайдено, ${r.competitors_failed} не вдалось`)
      }
    } catch (e) { setAnalysisMsg(String(e)) }
    finally { setAnalyzing(false) }
  }

  function applyAnalysis() {
    if (!analysisResult) return
    setDescription(analysisResult.description)
    setKeywords(analysisResult.keywords.join(', '))
    setAnalysisResult(null)
    setAnalysisMsg('')
  }

  async function apply() {
    setLoading(true); setMsg('')
    try {
      const kwList = keywords
        .split(',')
        .map((k) => k.trim())
        .filter(Boolean)

      const body: BrandingRequest = {
        description: description.trim() || null,
        keywords:    kwList.length ? kwList : null,
        country:     country.trim() || null,
        banner_path: bannerPath.trim() || null,
      }
      const j = await api.channels.applyBranding(channelName, body)
      setJob(j)
    } catch (e) {
      setMsg(String(e))
    } finally {
      setLoading(false)
    }
  }

  const jobColor =
    !job                    ? '' :
    job.status === 'done'   ? 'text-green-400' :
    job.status === 'failed' ? 'text-red-400'   :
    'text-yellow-400'

  return (
    <div className="space-y-5">

      {/* ── Competitor Analysis ──────────────────────────────────────────── */}
      <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-4 space-y-3">
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-wide">
          🔍 Аналіз конкурентів
        </p>
        <div className="space-y-1">
          <label className="text-xs text-gray-400 flex items-center gap-1.5">
            Посилання на канали конкурентів
            <Tip text="Кожне посилання з нового рядка. Підтримує: /@handle, /channel/UC…, /c/name, /user/name" />
          </label>
          <textarea
            value={competitorUrls}
            onChange={(e) => setCompetitorUrls(e.target.value)}
            rows={4}
            placeholder={
              'https://www.youtube.com/@HistoryChannel\n' +
              'https://www.youtube.com/@KingsAndGenerals\n' +
              'https://www.youtube.com/@OverSimplified'
            }
            className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-sm text-gray-200 font-mono focus:outline-none focus:border-purple-500 resize-y"
          />
        </div>
        <button
          onClick={analyze}
          disabled={analyzing || !competitorUrls.trim()}
          className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
        >
          {analyzing ? '⏳ Аналізуємо…' : '🔍 Аналізувати конкурентів'}
        </button>

        {analysisMsg && (
          <p className={`text-xs ${analysisMsg.startsWith('⚠️') ? 'text-yellow-400' : 'text-red-400'}`}>
            {analysisMsg}
          </p>
        )}

        {analysisResult && (
          <div className="bg-gray-900 rounded p-3 space-y-3 border border-purple-700/40">
            <p className="text-xs font-semibold text-gray-300">Результат аналізу:</p>
            <p className="text-xs text-gray-400 leading-relaxed italic">{analysisResult.analysis}</p>
            <div className="space-y-1">
              <p className="text-xs text-gray-500">Згенерований опис:</p>
              <p className="text-xs text-gray-200 bg-gray-800 rounded p-2 whitespace-pre-wrap">
                {analysisResult.description}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-xs text-gray-500">Ключові слова:</p>
              <p className="text-xs text-blue-300">{analysisResult.keywords.join(', ')}</p>
            </div>
            <p className="text-xs text-gray-500">
              Знайдено: {analysisResult.competitors_found}
              {analysisResult.competitors_failed > 0 && `, не вдалось: ${analysisResult.competitors_failed}`}
            </p>
            <button
              onClick={applyAnalysis}
              className="bg-green-700 hover:bg-green-600 text-white text-xs px-4 py-1.5 rounded"
            >
              ✓ Застосувати результат → заповнити поля нижче
            </button>
          </div>
        )}
      </div>

      {/* ── Branding fields ─────────────────────────────────────────────── */}

      {/* Description */}
      <div className="space-y-1">
        <label className="text-xs text-gray-400 flex items-center gap-1.5">
          Опис каналу <Tip text="До 1000 символів" />
        </label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={4}
          maxLength={1000}
          placeholder="Exploring the most fascinating stories from world history…"
          className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
        />
        <p className="text-xs text-gray-500 text-right">{description.length}/1000</p>
      </div>

      {/* Keywords */}
      <div className="space-y-1">
        <label className="text-xs text-gray-400 flex items-center gap-1.5">
          Ключові слова <Tip text="Через кому. Фрази з пробілами автоматично цитуються." />
        </label>
        <input
          value={keywords}
          onChange={(e) => setKeywords(e.target.value)}
          placeholder="history, world history, ancient rome, empires"
          className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Country */}
      <div className="space-y-1 w-24">
        <label className="text-xs text-gray-400">Країна</label>
        <input
          value={country}
          onChange={(e) => setCountry(e.target.value.toUpperCase())}
          maxLength={2}
          placeholder="UA"
          className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200 font-mono uppercase focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Banner */}
      <div className="space-y-1">
        <label className="text-xs text-gray-400 flex items-center gap-1.5">
          Баннер (шлях до файлу) <Tip text="PNG/JPG, мін. 2048×1152 px, до 6 МБ" />
        </label>
        <input
          value={bannerPath}
          onChange={(e) => setBannerPath(e.target.value)}
          placeholder="assets/branding/banner.png"
          className="w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-blue-500"
        />
        <p className="text-xs text-gray-500">
          Шлях відносно кореня проекту. Мін. 2048×1152 px, до 6 МБ.
          <span className="text-yellow-500 ml-1">Аватар потрібно завантажити вручну в YouTube Studio.</span>
        </p>
      </div>

      {/* Apply button + status */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={apply}
          disabled={loading || job?.status === 'running'}
          className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded"
        >
          {job?.status === 'running' ? '⏳ Застосовується…' : '📡 Застосувати до YouTube'}
        </button>
        {job && (
          <span className={`text-sm ${jobColor}`}>
            {job.status === 'done'   ? '✅ Оформлення застосовано!' :
             job.status === 'failed' ? `❌ ${job.error}` :
             '⏳ В процесі…'}
          </span>
        )}
        {msg && <span className="text-sm text-red-400">{msg}</span>}
      </div>

      <div className="text-xs text-gray-500 bg-gray-900 rounded p-3">
        <p>💡 Назву каналу та аватар встановіть вручну:</p>
        <a
          href="https://studio.youtube.com"
          target="_blank"
          rel="noreferrer"
          className="text-blue-400 hover:underline"
        >
          studio.youtube.com → Налаштування → Профіль
        </a>
      </div>
    </div>
  )
}

// ─── Thumbnail picker ─────────────────────────────────────────────────────────

function ThumbnailPicker({
  variants, selected, onSelect,
}: {
  variants: ThumbnailVariant[]
  selected: string | null
  onSelect: (f: string) => void
}) {
  if (!variants.length) return <p className="text-xs text-gray-500 italic">Немає превью</p>
  return (
    <div className="flex flex-wrap gap-2">
      {variants.map((v) => (
        <button
          key={v.filename} type="button" onClick={() => onSelect(v.filename)}
          className={`relative rounded overflow-hidden border-2 transition-all ${
            selected === v.filename
              ? 'border-blue-500 shadow-lg shadow-blue-500/30'
              : 'border-gray-600 hover:border-gray-400'
          }`}
        >
          <img src={v.url} alt={`#${v.index}`}
            className="w-32 h-[72px] object-cover"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <span className={`absolute bottom-0 left-0 right-0 text-center text-xs py-0.5 ${
            selected === v.filename ? 'bg-blue-600 text-white' : 'bg-black/60 text-gray-300'
          }`}>#{v.index}</span>
          {selected === v.filename && (
            <span className="absolute top-1 right-1 bg-blue-600 rounded-full w-4 h-4 flex items-center justify-center text-white text-xs">✓</span>
          )}
        </button>
      ))}
    </div>
  )
}

// ─── Upload section ───────────────────────────────────────────────────────────

function UploadSection({ channelConfigPath }: {
  channelConfigPath: string
}) {
  const [videos,     setVideos]     = useState<YoutubeReadyVideo[]>([])
  const [loading,    setLoading]    = useState(false)
  const [expanded,   setExpanded]   = useState<string | null>(null)
  const jobsRef = useRef<Record<string, YoutubeUploadJob>>({})
  const [jobs,       setJobs]       = useState<Record<string, YoutubeUploadJob>>({})

  async function loadVideos() {
    setLoading(true)
    try { setVideos(await api.youtube.ready()) }
    catch { /* ignore */ }
    finally { setLoading(false) }
  }

  useEffect(() => { loadVideos() }, [])

  function updateJob(dir: string, j: YoutubeUploadJob) {
    jobsRef.current = { ...jobsRef.current, [dir]: j }
    setJobs({ ...jobsRef.current })
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-400">{videos.length} відео готово до завантаження</span>
        <button onClick={loadVideos} className="text-xs text-gray-500 hover:text-white">↺ Refresh</button>
      </div>

      {loading && <p className="text-sm text-gray-400">Завантаження…</p>}
      {!loading && videos.length === 0 && (
        <p className="text-sm text-gray-500 italic">
          Немає відео. Згенеруйте відео через Jobs → крок 4+
        </p>
      )}

      {videos.map((v) => (
        <VideoUploadRow
          key={v.dir}
          video={v}
          channelConfigPath={channelConfigPath}
          job={jobs[v.dir] ?? null}
          onJobUpdate={(j) => updateJob(v.dir, j)}
          expanded={expanded === v.dir}
          onToggle={() => setExpanded(expanded === v.dir ? null : v.dir)}
        />
      ))}
    </div>
  )
}

interface UploadForm {
  selectedThumbnail: string | null
  selectedTitle:     string
  privacy:           'private' | 'unlisted' | 'public'
  schedule:          string
  dryRun:            boolean
}

function VideoUploadRow({ video, channelConfigPath, job, onJobUpdate, expanded, onToggle }: {
  video:             YoutubeReadyVideo
  channelConfigPath: string
  job:               YoutubeUploadJob | null
  onJobUpdate:       (j: YoutubeUploadJob) => void
  expanded:          boolean
  onToggle:          () => void
}) {
  const initTitle = video.title_variants?.[0] ?? video.title
  const [form, setForm] = useState<UploadForm>({
    selectedThumbnail: video.thumbnail_variants?.[0]?.filename ?? null,
    selectedTitle: initTitle,
    privacy: 'private',
    schedule: '',
    dryRun: false,
  })
  const [submitting, setSub] = useState(false)
  const [error, setError]    = useState('')

  // Poll job
  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'failed') return
    const iv = setInterval(async () => {
      try {
        const u = await api.youtube.uploadJob(job.job_id)
        onJobUpdate(u)
      } catch { /* ignore */ }
    }, 2000)
    return () => clearInterval(iv)
  }, [job, onJobUpdate])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(''); setSub(true)
    try {
      const j = await api.youtube.upload({
        project_dir:        video.dir,
        channel:            channelConfigPath,
        privacy:            form.privacy,
        schedule:           form.schedule || null,
        auto_schedule:      false,
        dry_run:            form.dryRun,
        selected_thumbnail: form.selectedThumbnail,
        selected_title:     form.selectedTitle || null,
      })
      onJobUpdate(j)
    } catch (err) { setError(String(err)) }
    finally { setSub(false) }
  }

  const isUploaded = !!video.uploaded
  const jobColor = !job ? '' :
    job.status === 'done'    ? 'text-green-400' :
    job.status === 'failed'  ? 'text-red-400'   :
    job.status === 'running' ? 'text-yellow-400' : 'text-gray-400'

  return (
    <div className={`bg-gray-800 rounded-lg border overflow-hidden ${isUploaded ? 'border-green-700/40' : 'border-gray-700'}`}>
      {/* Header */}
      <button type="button" onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-gray-750 text-left"
      >
        {/* Thumbnail preview */}
        {video.thumbnail_variants?.[0] ? (
          <img
            src={video.thumbnail_variants[0].url}
            alt=""
            className="w-20 h-11 object-cover rounded shrink-0"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        ) : (
          <div className="w-20 h-11 bg-gray-700 rounded shrink-0 flex items-center justify-center text-gray-500 text-xs">No img</div>
        )}

        <div className="flex-1 min-w-0">
          <p className="text-sm text-white truncate">{video.title}</p>
          <p className="text-xs text-gray-500">{video.video_size_mb} MB · {video.tags_count} tags</p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {isUploaded && (
            <a
              href={`https://www.youtube.com/watch?v=${video.uploaded?.video_id}`}
              target="_blank" rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-xs text-green-400 hover:underline"
            >
              ✅ Завантажено
            </a>
          )}
          {job && (
            <span className={`text-xs ${jobColor}`}>
              {job.status === 'done' ? '✅ Done' : job.status === 'failed' ? '❌ Failed' : '⏳…'}
            </span>
          )}
          <span className="text-gray-500 text-xs">{expanded ? '▲' : '▼'}</span>
        </div>
      </button>

      {/* Expandable upload form */}
      {expanded && (
        <form onSubmit={submit} className="border-t border-gray-700 px-4 py-4 space-y-4">
          {/* Thumbnail picker */}
          {video.thumbnail_variants?.length > 0 && (
            <div className="space-y-1.5">
              <label className="text-xs text-gray-400">Оберіть thumbnail</label>
              <ThumbnailPicker
                variants={video.thumbnail_variants}
                selected={form.selectedThumbnail}
                onSelect={(f) => setForm({ ...form, selectedThumbnail: f })}
              />
            </div>
          )}

          {/* Title selector */}
          {(video.title_variants?.length ?? 0) > 1 && (
            <div className="space-y-1.5">
              <label className="text-xs text-gray-400">Оберіть назву</label>
              <div className="space-y-1">
                {video.title_variants!.map((t, i) => (
                  <label key={i}
                    className={`flex items-start gap-2 p-2 rounded border cursor-pointer text-sm ${
                      form.selectedTitle === t
                        ? 'border-blue-500 bg-blue-500/10 text-white'
                        : 'border-gray-600 hover:border-gray-500 text-gray-300'
                    }`}
                  >
                    <input type="radio" name="title" checked={form.selectedTitle === t}
                      onChange={() => setForm({ ...form, selectedTitle: t })}
                      className="mt-0.5 accent-blue-500 shrink-0"
                    />
                    <span><span className="text-xs text-gray-500 mr-1">#{i + 1}</span>{t}</span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* Privacy + Schedule */}
          <div className="flex gap-3 flex-wrap">
            <div className="space-y-1">
              <label className="text-xs text-gray-400">Доступ</label>
              <select
                value={form.privacy}
                onChange={(e) => setForm({ ...form, privacy: e.target.value as UploadForm['privacy'] })}
                className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
              >
                <option value="private">Private</option>
                <option value="unlisted">Unlisted</option>
                <option value="public">Public</option>
              </select>
            </div>
            <div className="space-y-1 flex-1">
              <label className="text-xs text-gray-400 flex items-center gap-1.5">
                Публікація <Tip text="Залиште порожнім для ручного планування в Studio" />
              </label>
              <input
                type="datetime-local"
                value={form.schedule}
                onChange={(e) => setForm({ ...form, schedule: e.target.value })}
                className="bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          {/* Dry run */}
          <label className="flex items-center gap-2 cursor-pointer text-sm text-gray-300">
            <input type="checkbox" checked={form.dryRun}
              onChange={(e) => setForm({ ...form, dryRun: e.target.checked })}
              className="accent-blue-500"
            />
            Dry run (без реального завантаження)
          </label>

          {/* Submit */}
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={submitting || job?.status === 'running'}
              className="bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white text-sm font-medium px-5 py-2 rounded"
            >
              {submitting || job?.status === 'running' ? '⏳ Завантажується…' : '▲ Завантажити на YouTube'}
            </button>
            {job?.status === 'failed' && (
              <span className="text-red-400 text-xs">{job.error?.slice(0, 120)}</span>
            )}
            {job?.status === 'done' && job.result && (
              <a
                href={`https://www.youtube.com/watch?v=${job.result.video_id}`}
                target="_blank" rel="noreferrer"
                className="text-green-400 text-sm hover:underline"
              >
                ✅ Переглянути відео
              </a>
            )}
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
        </form>
      )}
    </div>
  )
}

// ─── Main panel ───────────────────────────────────────────────────────────────

type SectionTab = 'auth' | 'branding' | 'upload'

const SECTION_TABS: { id: SectionTab; label: string; emoji: string }[] = [
  { id: 'auth',     label: "З'єднання", emoji: '🔗' },
  { id: 'branding', label: 'Оформлення', emoji: '🎨' },
  { id: 'upload',   label: 'Відео',      emoji: '📤' },
]

export function YoutubePanel() {
  const [channels,      setChannels]      = useState<ChannelMeta[]>([])
  const [selected,      setSelected]      = useState<string | null>(null)
  const [channelData,   setChannelData]   = useState<Record<string, unknown>>({})
  const [sectionTab,    setSectionTab]    = useState<SectionTab>('auth')
  const [loadingDetail, setLoadingDetail] = useState(false)

  async function loadChannels() {
    try {
      const list = await api.channels.list()
      setChannels(list)
      if (list.length > 0 && !selected) setSelected(list[0].name)
    } catch { /* ignore */ }
  }

  useEffect(() => { loadChannels() }, [])

  useEffect(() => {
    if (!selected) return
    setLoadingDetail(true)
    api.channels.get(selected)
      .then(setChannelData)
      .catch(() => setChannelData({}))
      .finally(() => setLoadingDetail(false))
  }, [selected])

  const selectedMeta = channels.find((c) => c.name === selected)
  const channelConfigPath = selected ? `config/channels/${selected}.json` : ''

  return (
    <div className="flex h-[calc(100vh-140px)] bg-gray-850">
      {/* Left sidebar */}
      <ChannelSidebar
        channels={channels}
        selected={selected}
        onSelect={(n) => { setSelected(n); setSectionTab('auth') }}
        onRefresh={loadChannels}
      />

      {/* Right panel */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">
            Оберіть канал зліва
          </div>
        ) : (
          <>
            {/* Channel header */}
            <div className="px-5 py-3 border-b border-gray-700 flex items-center gap-3">
              <Dot on={selectedMeta?.auth_connected ?? false} />
              <span className="font-semibold text-white text-base">
                {selectedMeta?.channel_name || selected}
              </span>
              <span className="text-xs text-gray-500">{selected}.json</span>
              {selectedMeta?.proxy && (
                <span className="text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">proxy</span>
              )}
            </div>

            {/* Section tabs */}
            <div className="flex border-b border-gray-700 px-5">
              {SECTION_TABS.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setSectionTab(tab.id)}
                  className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                    sectionTab === tab.id
                      ? 'border-blue-500 text-white'
                      : 'border-transparent text-gray-400 hover:text-gray-200'
                  }`}
                >
                  {tab.emoji} {tab.label}
                </button>
              ))}
            </div>

            {/* Section content */}
            <div className="flex-1 overflow-y-auto p-5">
              {loadingDetail ? (
                <p className="text-gray-400 text-sm">Завантаження…</p>
              ) : sectionTab === 'auth' ? (
                <AuthSection
                  channelName={selected}
                  channelData={channelData}
                  onSaved={loadChannels}
                />
              ) : sectionTab === 'branding' ? (
                <BrandingSection
                  channelName={selected}
                  channelData={channelData}
                />
              ) : (
                <UploadSection
                  channelConfigPath={channelConfigPath}
                />
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
