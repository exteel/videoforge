import { useState } from 'react'
import { JobList } from './components/JobList'
import { VideoList } from './components/VideoList'
import { StatsPanel } from './components/StatsPanel'

type Tab = 'jobs' | 'history' | 'stats'

const TABS: { id: Tab; label: string }[] = [
  { id: 'jobs',    label: 'Jobs' },
  { id: 'history', label: 'History' },
  { id: 'stats',   label: 'Stats' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('jobs')

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Navbar */}
      <header className="bg-gray-800 border-b border-gray-700 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 flex items-center gap-6 h-12">
          <span className="font-bold text-sm tracking-tight text-white">VideoForge</span>
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
                {t.label}
              </button>
            ))}
          </nav>
          <div className="ml-auto text-xs text-gray-500">
            <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer" className="hover:text-gray-300">
              API docs ↗
            </a>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-5xl mx-auto px-4 py-6">
        {tab === 'jobs'    && <JobList />}
        {tab === 'history' && <VideoList />}
        {tab === 'stats'   && <StatsPanel />}
      </main>
    </div>
  )
}
