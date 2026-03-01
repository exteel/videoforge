import { useEffect, useState } from 'react'
import { type Job, type PipelineRunRequest, type BatchRunRequest, api } from '../api'
import { JobCard } from './JobCard'

const QUALITY_OPTS = ['max', 'high', 'balanced', 'bulk', 'test']
const TEMPLATE_OPTS = ['auto', 'documentary', 'listicle', 'tutorial', 'comparison']

export function JobList() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'pipeline' | 'batch'>('pipeline')

  // Pipeline form
  const [pForm, setPForm] = useState<PipelineRunRequest>({
    source_dir: '',
    channel: 'config/channels/history.json',
    quality: 'max',
    template: 'auto',
    dry_run: false,
    draft: false,
    from_step: 1,
  })

  // Batch form
  const [bForm, setBForm] = useState<BatchRunRequest>({
    input_dir: '',
    channel: 'config/channels/history.json',
    quality: 'bulk',
    parallel: 1,
    dry_run: false,
    skip_done: true,
  })

  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')

  async function loadJobs() {
    try {
      const data = await api.jobs.list(100)
      setJobs(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadJobs()
    const t = setInterval(loadJobs, 3000)
    return () => clearInterval(t)
  }, [])

  async function submitPipeline(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    setSubmitting(true)
    try {
      await api.pipeline.run(pForm)
      await loadJobs()
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function submitBatch(e: React.FormEvent) {
    e.preventDefault()
    setFormError('')
    setSubmitting(true)
    try {
      await api.batch.run(bForm)
      await loadJobs()
    } catch (err) {
      setFormError(String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const activeJobs = jobs.filter((j) => j.status === 'running' || j.status === 'queued')
  const recentJobs = jobs.filter((j) => j.status !== 'running' && j.status !== 'queued').slice(0, 20)

  return (
    <div className="space-y-6">
      {/* Launch form */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <div className="flex gap-2 mb-4">
          {(['pipeline', 'batch'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                tab === t ? 'bg-blue-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              {t === 'pipeline' ? 'Single Video' : 'Batch'}
            </button>
          ))}
        </div>

        {tab === 'pipeline' ? (
          <form onSubmit={submitPipeline} className="space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Source dir *</span>
                <input
                  required
                  value={pForm.source_dir}
                  onChange={(e) => setPForm({ ...pForm, source_dir: e.target.value })}
                  placeholder="D:/transscript batch/output/output/Video Title"
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Channel config</span>
                <input
                  value={pForm.channel}
                  onChange={(e) => setPForm({ ...pForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Quality</span>
                <select
                  value={pForm.quality}
                  onChange={(e) => setPForm({ ...pForm, quality: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {QUALITY_OPTS.map((q) => <option key={q}>{q}</option>)}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Template</span>
                <select
                  value={pForm.template}
                  onChange={(e) => setPForm({ ...pForm, template: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {TEMPLATE_OPTS.map((t) => <option key={t}>{t}</option>)}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">From step (1–6)</span>
                <input
                  type="number"
                  min={1}
                  max={6}
                  value={pForm.from_step}
                  onChange={(e) => setPForm({ ...pForm, from_step: Number(e.target.value) })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Budget (USD, optional)</span>
                <input
                  type="number"
                  step="0.01"
                  placeholder="e.g. 5.00"
                  value={pForm.budget ?? ''}
                  onChange={(e) => setPForm({ ...pForm, budget: e.target.value ? Number(e.target.value) : null })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                />
              </label>
            </div>
            <div className="flex gap-4 text-sm text-gray-300">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.draft} onChange={(e) => setPForm({ ...pForm, draft: e.target.checked })} className="accent-blue-500" />
                Draft (480p)
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={pForm.dry_run} onChange={(e) => setPForm({ ...pForm, dry_run: e.target.checked })} className="accent-blue-500" />
                Dry run
              </label>
            </div>
            {formError && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>}
            <button
              type="submit"
              disabled={submitting}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded transition-colors"
            >
              {submitting ? 'Starting…' : 'Run Pipeline'}
            </button>
          </form>
        ) : (
          <form onSubmit={submitBatch} className="space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Input dir *</span>
                <input
                  required
                  value={bForm.input_dir}
                  onChange={(e) => setBForm({ ...bForm, input_dir: e.target.value })}
                  placeholder="D:/transscript batch/output/output"
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Channel config</span>
                <input
                  value={bForm.channel}
                  onChange={(e) => setBForm({ ...bForm, channel: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Quality</span>
                <select
                  value={bForm.quality}
                  onChange={(e) => setBForm({ ...bForm, quality: e.target.value })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {QUALITY_OPTS.map((q) => <option key={q}>{q}</option>)}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Parallel workers</span>
                <input
                  type="number"
                  min={1}
                  max={8}
                  value={bForm.parallel}
                  onChange={(e) => setBForm({ ...bForm, parallel: Number(e.target.value) })}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
            </div>
            <div className="flex gap-4 text-sm text-gray-300">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={bForm.skip_done} onChange={(e) => setBForm({ ...bForm, skip_done: e.target.checked })} className="accent-blue-500" />
                Skip done
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={bForm.dry_run} onChange={(e) => setBForm({ ...bForm, dry_run: e.target.checked })} className="accent-blue-500" />
                Dry run
              </label>
            </div>
            {formError && <div className="text-xs text-red-300 bg-red-950 rounded p-2">{formError}</div>}
            <button
              type="submit"
              disabled={submitting}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded transition-colors"
            >
              {submitting ? 'Starting…' : 'Run Batch'}
            </button>
          </form>
        )}
      </div>

      {/* Active jobs */}
      {activeJobs.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">Active ({activeJobs.length})</h2>
          {activeJobs.map((j) => <JobCard key={j.job_id} job={j} onRefresh={loadJobs} />)}
        </div>
      )}

      {/* Recent jobs */}
      {recentJobs.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">Recent</h2>
          {recentJobs.map((j) => <JobCard key={j.job_id} job={j} onRefresh={loadJobs} />)}
        </div>
      )}

      {!loading && jobs.length === 0 && (
        <p className="text-gray-500 text-sm text-center py-8">No jobs yet. Run a pipeline above.</p>
      )}
    </div>
  )
}
