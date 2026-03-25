import { useState, useEffect, useCallback } from 'react'
import { JobList } from './components/JobList'
import { ScriptEditor } from './components/ScriptEditor'
import { VideoList } from './components/VideoList'
import { StatsPanel } from './components/StatsPanel'
import { ChannelsPanel } from './components/ChannelsPanel'
import { YoutubePanel } from './components/YoutubePanel'
import { PresetsPanel } from './components/PresetsPanel'
import { Login } from './components/Login'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { useNotifications } from './hooks/useNotifications'

type Tab = 'jobs' | 'script' | 'channels' | 'presets' | 'history' | 'youtube' | 'stats'

const TABS: { id: Tab; label: string; emoji: string }[] = [
  { id: 'jobs',     label: 'Jobs',     emoji: '▶' },
  { id: 'script',   label: 'Script',   emoji: '📝' },
  { id: 'channels', label: 'Channels', emoji: '📡' },
  { id: 'presets',  label: 'Presets',  emoji: '⚙' },
  { id: 'history',  label: 'History',  emoji: '🎬' },
  { id: 'youtube',  label: 'YouTube',  emoji: '▲' },
  { id: 'stats',    label: 'Stats',    emoji: '📊' },
]

const TAB_DESC: Record<Tab, { title: string; desc: string }> = {
  jobs: {
    title: 'Запуск пайплайну',
    desc:  'Запускайте генерацію одного відео або батч із кількох. Прогрес оновлюється в реальному часі через WebSocket. Після Step 1 (Script) пайплайн зупиняється для ревью — переходьте на вкладку Script перед продовженням.',
  },
  script: {
    title: 'Редактор сценарію',
    desc:  'Переглядайте та редагуйте script.json після Step 1. Це точка контролю перед витратами на зображення та озвучку. Змініть наративи, image prompts або структуру блоків, потім запустіть пайплайн з Step 2.',
  },
  channels: {
    title: 'Канали та промпти',
    desc:  'Налаштовуйте конфіги каналів: ніша, мова, голос TTS, стиль зображень, LLM-пресети. Редагуйте мастер-промпти для різних ніш (психологія, фінанси, історія тощо). Зміни зберігаються у config/channels/ та prompts/.',
  },
  presets: {
    title: 'Пресети налаштувань',
    desc:  'Збережіть типові конфігурації форми (канал, якість, тривалість, бекенди тощо). Оберіть пресет у формі Jobs — всі поля заповняться автоматично.',
  },
  history: {
    title: 'Історія відео',
    desc:  'Усі згенеровані відео з деталями витрат по кожному кроку пайплайну. Клікніть на відео для перегляду повного breakdown витрат та посилання на YouTube.',
  },
  youtube: {
    title: 'Завантаження на YouTube',
    desc:  'OAuth2-авторизація + завантаження відео з projects/. Підтримує scheduling, auto-schedule за channel config, dry run. Private + schedule є найбезпечнішим способом для нових каналів.',
  },
  stats: {
    title: 'Статистика',
    desc:  'Зведена аналітика: загальні витрати, середній час генерації, розбивка по AI-моделях та якісних пресетах. Допомагає оптимізувати вибір моделей та бюджет.',
  },
}

function AppShell() {
  const { ready, authenticated } = useAuth()
  if (!ready) return (
    <div className="min-h-screen bg-gray-900 flex items-center justify-center">
      <span className="text-gray-500 text-sm">Loading…</span>
    </div>
  )
  if (!authenticated) return <Login />
  return <AppMain />
}

function useTunnelUrl() {
  const [url, setUrl] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const fetch = useCallback(async () => {
    try {
      const r = await window.fetch('/api/tunnel')
      const data = await r.json() as { url: string | null }
      setUrl(data.url ?? null)
    } catch { /* tunnel not running */ }
  }, [])

  useEffect(() => {
    fetch()
    const id = setInterval(fetch, 30_000)
    return () => clearInterval(id)
  }, [fetch])

  async function copy() {
    if (!url) return
    await navigator.clipboard.writeText(url)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return { url, copied, copy, refresh: fetch }
}

function AppMain() {
  const { protected: isProtected, logout } = useAuth()
  const [tab, setTab] = useState<Tab>('jobs')
  const { title, desc } = TAB_DESC[tab]
  const { permission, requestPermission } = useNotifications()
  const ngrok = useTunnelUrl()

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Navbar */}
      <header className="bg-gray-800 border-b border-gray-700 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-3 md:px-4 flex items-center gap-3 md:gap-6 h-12">
          <span className="font-bold text-sm tracking-tight text-white shrink-0">
            🎬 <span className="hidden sm:inline">VideoForge</span>
          </span>

          {/* Tunnel public URL — hidden on small mobile */}
          {ngrok.url ? (
            <button
              onClick={ngrok.copy}
              title="Клікни щоб скопіювати посилання для співробітників"
              className="flex items-center gap-1.5 px-2 py-1 rounded bg-green-900/50 border border-green-700/50 hover:bg-green-800/60 transition-colors group shrink-0"
            >
              <span className="text-green-400 text-xs">🌐</span>
              <span className="font-mono text-xs text-green-300 max-w-[120px] md:max-w-[220px] truncate">
                {ngrok.url.replace('https://', '')}
              </span>
              <span className="text-[10px] text-green-500 group-hover:text-green-300 transition-colors">
                {ngrok.copied ? '✓' : '⎘'}
              </span>
            </button>
          ) : (
            <span className="text-xs text-gray-600 shrink-0 hidden sm:inline">тунель вимк.</span>
          )}

          {/* Desktop tabs — hidden on mobile */}
          <nav className="hidden md:flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                  tab === t.id
                    ? 'bg-gray-700 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-gray-700/50'
                }`}
              >
                {t.emoji} {t.label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 md:gap-3">
            {/* Notification bell */}
            {permission !== 'granted' && (
              <button
                onClick={permission === 'default' ? requestPermission : undefined}
                disabled={permission === 'denied'}
                title={
                  permission === 'denied'
                    ? 'Сповіщення заблоковані — дозвольте в налаштуваннях браузера'
                    : 'Увімкнути browser-сповіщення'
                }
                className={`text-sm transition-colors ${
                  permission === 'denied'
                    ? 'text-gray-600 cursor-not-allowed'
                    : 'text-amber-400 hover:text-amber-300 hover:bg-gray-700/50 px-1.5 py-0.5 rounded cursor-pointer'
                }`}
              >
                {permission === 'denied' ? '🔕' : '🔔'}
              </button>
            )}
            <span className="text-xs text-gray-500 hidden md:inline">
              <a
                href="http://localhost:8000/docs"
                target="_blank"
                rel="noreferrer"
                className="hover:text-gray-300 transition-colors"
              >
                API docs ↗
              </a>
            </span>
            {isProtected && (
              <button
                onClick={logout}
                title="Вийти"
                className="text-xs text-gray-500 hover:text-red-400 transition-colors"
              >
                <span className="hidden sm:inline">Вийти</span>
                <span className="sm:hidden">✕</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Page description banner — hidden on mobile */}
      <div className="hidden md:block bg-gray-800/50 border-b border-gray-700/50">
        <div className="max-w-5xl mx-auto px-4 py-3">
          <div className="flex items-baseline gap-3">
            <span className="text-sm font-semibold text-white">{title}</span>
            <span className="text-xs text-gray-400 leading-relaxed">{desc}</span>
          </div>
        </div>
      </div>

      {/* Mobile tab title */}
      <div className="md:hidden bg-gray-800/30 border-b border-gray-700/30 px-3 py-2">
        <span className="text-sm font-semibold text-white">{TAB_DESC[tab].title}</span>
      </div>

      {/* Content — bottom padding on mobile for bottom nav */}
      <main className="max-w-5xl mx-auto px-3 md:px-4 py-4 md:py-6 pb-20 md:pb-6">
        {tab === 'jobs'     && <JobList />}
        {tab === 'script'   && <ScriptEditor />}
        {tab === 'channels' && <ChannelsPanel />}
        {tab === 'presets'  && <PresetsPanel />}
        {tab === 'history'  && <VideoList />}
        {tab === 'youtube'  && <YoutubePanel />}
        {tab === 'stats'    && <StatsPanel />}
      </main>

      {/* Mobile bottom navigation */}
      <nav className="md:hidden fixed bottom-0 left-0 right-0 bg-gray-800 border-t border-gray-700 z-20 flex">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex-1 flex flex-col items-center justify-center py-2 gap-0.5 transition-colors ${
              tab === t.id
                ? 'text-white bg-gray-700/50'
                : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            <span className="text-base leading-none">{t.emoji}</span>
            <span className="text-[9px] leading-none mt-0.5 truncate w-full text-center px-0.5">
              {t.label}
            </span>
          </button>
        ))}
      </nav>
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}
