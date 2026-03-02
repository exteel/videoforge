import { useState } from 'react'
import { JobList } from './components/JobList'
import { ScriptEditor } from './components/ScriptEditor'
import { VideoList } from './components/VideoList'
import { StatsPanel } from './components/StatsPanel'
import { ChannelsPanel } from './components/ChannelsPanel'
import { YoutubePanel } from './components/YoutubePanel'
import { useNotifications } from './hooks/useNotifications'

type Tab = 'jobs' | 'script' | 'channels' | 'history' | 'youtube' | 'stats'

const TABS: { id: Tab; label: string; emoji: string }[] = [
  { id: 'jobs',     label: 'Jobs',     emoji: '▶' },
  { id: 'script',   label: 'Script',   emoji: '📝' },
  { id: 'channels', label: 'Channels', emoji: '📡' },
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

export default function App() {
  const [tab, setTab] = useState<Tab>('jobs')
  const { title, desc } = TAB_DESC[tab]
  const { permission, requestPermission } = useNotifications()

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Navbar */}
      <header className="bg-gray-800 border-b border-gray-700 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 flex items-center gap-6 h-12">
          <span className="font-bold text-sm tracking-tight text-white">
            🎬 VideoForge
          </span>
          <nav className="flex gap-1">
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
          <div className="ml-auto flex items-center gap-3">
            {/* Notification permission bell */}
            {permission !== 'granted' && (
              <button
                onClick={permission === 'default' ? requestPermission : undefined}
                disabled={permission === 'denied'}
                title={
                  permission === 'denied'
                    ? 'Сповіщення заблоковані — дозвольте в налаштуваннях браузера'
                    : 'Увімкнути browser-сповіщення для review checkpoints та завершення задач'
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
            <span className="text-xs text-gray-500">
              <a
                href="http://localhost:8000/docs"
                target="_blank"
                rel="noreferrer"
                className="hover:text-gray-300 transition-colors"
              >
                API docs ↗
              </a>
            </span>
          </div>
        </div>
      </header>

      {/* Page description banner */}
      <div className="bg-gray-800/50 border-b border-gray-700/50">
        <div className="max-w-5xl mx-auto px-4 py-3">
          <div className="flex items-baseline gap-3">
            <span className="text-sm font-semibold text-white">{title}</span>
            <span className="text-xs text-gray-400 leading-relaxed">{desc}</span>
          </div>
        </div>
      </div>

      {/* Content */}
      <main className="max-w-5xl mx-auto px-4 py-6">
        {tab === 'jobs'     && <JobList />}
        {tab === 'script'   && <ScriptEditor />}
        {tab === 'channels' && <ChannelsPanel />}
        {tab === 'history'  && <VideoList />}
        {tab === 'youtube'  && <YoutubePanel />}
        {tab === 'stats'    && <StatsPanel />}
      </main>
    </div>
  )
}
