import { useState, useCallback } from 'react'
import { type Script, type ScriptBlock, api } from '../api'

const BLOCK_TYPE_COLOR: Record<string, string> = {
  intro:   'bg-purple-900 text-purple-300',
  section: 'bg-blue-900 text-blue-300',
  cta:     'bg-orange-900 text-orange-300',
  outro:   'bg-gray-700 text-gray-300',
}

const QUALITY_OPTS = ['max', 'high', 'balanced', 'bulk', 'test']
const TEMPLATE_OPTS = ['auto', 'documentary', 'listicle', 'tutorial', 'comparison']

function BlockCard({
  block,
  index,
  onChange,
}: {
  block: ScriptBlock
  index: number
  onChange: (idx: number, patch: Partial<ScriptBlock>) => void
}) {
  const dur = block.audio_duration != null
    ? `${block.audio_duration.toFixed(1)}s`
    : '—'

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 font-mono w-5">{block.order}</span>
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${BLOCK_TYPE_COLOR[block.type] ?? 'bg-gray-700 text-gray-300'}`}>
          {block.type}
        </span>
        {block.timestamp_label && (
          <span className="text-xs text-gray-500">{block.timestamp_label}</span>
        )}
        <span className="ml-auto text-xs text-gray-600">{dur}</span>
      </div>

      {/* Narration */}
      <div className="space-y-1">
        <label className="text-xs text-gray-400">Narration</label>
        <textarea
          value={block.narration}
          onChange={(e) => onChange(index, { narration: e.target.value })}
          rows={4}
          className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500 resize-y"
        />
        <div className="text-xs text-gray-600 text-right">{block.narration.length} chars</div>
      </div>

      {/* Image prompt */}
      {block.type !== 'cta' && (
        <div className="space-y-1">
          <label className="text-xs text-gray-400">Image prompt</label>
          <input
            type="text"
            value={block.image_prompt}
            onChange={(e) => onChange(index, { image_prompt: e.target.value })}
            className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
            placeholder="Describe the image for this block…"
          />
        </div>
      )}
    </div>
  )
}

export function ScriptEditor() {
  const [sourceDir, setSourceDir] = useState('')
  const [script, setScript] = useState<Script | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [launching, setLaunching] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  // Launch settings (after load, pre-filled from script)
  const [channel, setChannel] = useState('config/channels/history.json')
  const [quality, setQuality] = useState('max')
  const [template, setTemplate] = useState('auto')
  const [fromStep, setFromStep] = useState(2)
  const [launchMsg, setLaunchMsg] = useState('')

  // ── Load ──────────────────────────────────────────────────────────────────

  async function handleLoad() {
    if (!sourceDir.trim()) return
    setError('')
    setLoading(true)
    setScript(null)
    setDirty(false)
    setSaved(false)
    setLaunchMsg('')
    try {
      const data = await api.script.get(sourceDir.trim())
      setScript(data)
      // Pre-fill template from loaded script
      if (data.niche) setTemplate('auto')
    } catch (err) {
      setError(String(err))
    } finally {
      setLoading(false)
    }
  }

  // ── Edit blocks ───────────────────────────────────────────────────────────

  const handleBlockChange = useCallback(
    (idx: number, patch: Partial<ScriptBlock>) => {
      setScript((prev) => {
        if (!prev) return prev
        const blocks = prev.blocks.map((b, i) =>
          i === idx ? { ...b, ...patch } : b
        )
        return { ...prev, blocks }
      })
      setDirty(true)
      setSaved(false)
    },
    []
  )

  // ── Save ──────────────────────────────────────────────────────────────────

  async function handleSave() {
    if (!script) return
    setSaving(true)
    setError('')
    try {
      await api.script.save(sourceDir.trim(), script)
      setDirty(false)
      setSaved(true)
    } catch (err) {
      setError(String(err))
    } finally {
      setSaving(false)
    }
  }

  // ── Launch pipeline from step N ───────────────────────────────────────────

  async function handleLaunch() {
    if (!sourceDir.trim()) return
    if (dirty) {
      setError('Save the script first before launching.')
      return
    }
    setLaunching(true)
    setError('')
    setLaunchMsg('')
    try {
      const job = await api.pipeline.run({
        source_dir: sourceDir.trim(),
        channel,
        quality,
        template,
        from_step: fromStep,
      })
      setLaunchMsg(`Job started: ${job.job_id} — check the Jobs tab for progress.`)
    } catch (err) {
      setError(String(err))
    } finally {
      setLaunching(false)
    }
  }

  // ── Derived stats ─────────────────────────────────────────────────────────

  const totalDur = script
    ? script.blocks.reduce((s, b) => s + (b.audio_duration ?? 0), 0)
    : 0
  const totalChars = script
    ? script.blocks.reduce((s, b) => s + b.narration.length, 0)
    : 0

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">

      {/* ── Source dir loader ── */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
        <h2 className="text-sm font-semibold text-gray-300">Load script</h2>
        <div className="flex gap-2">
          <input
            type="text"
            value={sourceDir}
            onChange={(e) => setSourceDir(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleLoad()}
            placeholder='D:/transscript batch/output/output/Video Title'
            className="flex-1 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500"
          />
          <button
            onClick={handleLoad}
            disabled={loading || !sourceDir.trim()}
            className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-1.5 rounded transition-colors"
          >
            {loading ? 'Loading…' : 'Load'}
          </button>
        </div>
        {error && (
          <div className="text-xs text-red-300 bg-red-950 rounded p-2">{error}</div>
        )}
      </div>

      {/* ── Script header + actions ── */}
      {script && (
        <>
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
            {/* Title & stats */}
            <div className="flex items-start gap-3 flex-wrap">
              <div className="flex-1 min-w-0">
                <div className="text-white font-semibold text-base truncate">{script.title}</div>
                <div className="text-xs text-gray-400 mt-0.5 flex gap-3 flex-wrap">
                  <span>{script.blocks.length} blocks</span>
                  <span>{(totalDur / 60).toFixed(1)} min</span>
                  <span>{totalChars.toLocaleString()} chars</span>
                  <span className="font-mono">{script.language}</span>
                  {script.niche && <span>{script.niche}</span>}
                </div>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleSave}
                  disabled={saving || !dirty}
                  className={`text-sm font-medium px-4 py-1.5 rounded transition-colors ${
                    dirty
                      ? 'bg-green-700 hover:bg-green-600 text-white'
                      : 'bg-gray-700 text-gray-500 cursor-default'
                  } disabled:opacity-50`}
                >
                  {saving ? 'Saving…' : saved ? 'Saved ✓' : 'Save'}
                </button>
              </div>
            </div>

            {/* Tags */}
            {script.tags.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {script.tags.map((t) => (
                  <span key={t} className="text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded">
                    {t}
                  </span>
                ))}
              </div>
            )}

            {/* Thumbnail prompt */}
            {script.thumbnail_prompt && (
              <div className="text-xs text-gray-500 border-t border-gray-700 pt-2">
                <span className="text-gray-400 font-medium">Thumbnail: </span>
                {script.thumbnail_prompt}
              </div>
            )}
          </div>

          {/* ── Block cards ── */}
          <div className="space-y-3">
            {script.blocks.map((block, idx) => (
              <BlockCard
                key={block.id}
                block={block}
                index={idx}
                onChange={handleBlockChange}
              />
            ))}
          </div>

          {/* ── Launch settings ── */}
          <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-3">
            <h2 className="text-sm font-semibold text-gray-300">Continue pipeline</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <label className="space-y-1 col-span-2 md:col-span-2">
                <span className="text-xs text-gray-400">Channel config</span>
                <input
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Quality</span>
                <select
                  value={quality}
                  onChange={(e) => setQuality(e.target.value)}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {QUALITY_OPTS.map((q) => <option key={q}>{q}</option>)}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400">Template</span>
                <select
                  value={template}
                  onChange={(e) => setTemplate(e.target.value)}
                  className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  {TEMPLATE_OPTS.map((t) => <option key={t}>{t}</option>)}
                </select>
              </label>
            </div>
            <div className="flex items-end gap-3">
              <label className="space-y-1">
                <span className="text-xs text-gray-400">From step</span>
                <input
                  type="number"
                  min={1}
                  max={6}
                  value={fromStep}
                  onChange={(e) => setFromStep(Number(e.target.value))}
                  className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </label>
              <button
                onClick={handleLaunch}
                disabled={launching || dirty}
                className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-1.5 rounded transition-colors"
                title={dirty ? 'Save the script first' : ''}
              >
                {launching ? 'Starting…' : `Run from step ${fromStep} →`}
              </button>
            </div>
            {dirty && (
              <p className="text-xs text-yellow-400">Save the script before launching.</p>
            )}
            {launchMsg && (
              <p className="text-xs text-green-400">{launchMsg}</p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
