import { useEffect, useState } from 'react'
import { type ChannelMeta, type PromptMeta, api } from '../api'

// ── helpers ───────────────────────────────────────────────────────────────────

function fmt(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}

// ── Channel editor ─────────────────────────────────────────────────────────────

interface ChannelEditorProps {
  name: string
  onClose: () => void
  onSaved: () => void
}

function ChannelEditor({ name, onClose, onSaved }: ChannelEditorProps) {
  const [raw, setRaw]     = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [jsonError, setJsonError] = useState('')

  useEffect(() => {
    api.channels.get(name)
      .then((data) => {
        setRaw(JSON.stringify(data, null, 2))
        setLoading(false)
      })
      .catch((e) => { setError(String(e)); setLoading(false) })
  }, [name])

  function handleChange(val: string) {
    setRaw(val)
    try { JSON.parse(val); setJsonError('') }
    catch { setJsonError('Invalid JSON') }
  }

  async function handleSave() {
    try {
      const data = JSON.parse(raw)
      setSaving(true)
      await api.channels.save(name, data)
      onSaved()
      onClose()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="text-gray-400 text-sm p-4">Loading…</div>
  if (error)   return <div className="text-red-400 text-sm p-4">{error}</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">{name}.json</h3>
        <button onClick={onClose} className="text-gray-500 hover:text-white text-xs">✕ close</button>
      </div>
      <textarea
        value={raw}
        onChange={(e) => handleChange(e.target.value)}
        rows={24}
        className="w-full bg-gray-950 border border-gray-600 rounded px-3 py-2 text-xs font-mono text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
        spellCheck={false}
      />
      {jsonError && <p className="text-xs text-red-400">{jsonError}</p>}
      <div className="flex gap-2">
        <button
          onClick={handleSave}
          disabled={saving || !!jsonError}
          className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-medium px-4 py-1.5 rounded"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xs px-3 py-1.5">
          Cancel
        </button>
      </div>
    </div>
  )
}

// ── New channel form ───────────────────────────────────────────────────────────

const CHANNEL_TEMPLATE = {
  channel_name: 'My Channel',
  niche: 'history',
  language: 'en',
  voice_id: 'a4CnuaYbALRvW39mDitg',
  image_style: 'cinematic, photorealistic, dramatic lighting, 8k, high detail',
  thumbnail_style: 'bold text overlay, dramatic imagery, high contrast, vibrant colors',
  subtitle_style: {
    font: 'Arial Bold', size: 48, color: '#FFFFFF',
    outline_color: '#000000', outline_width: 3,
    position: 'bottom', margin_v: 60,
  },
  default_animation: 'zoom_in',
  master_prompt_path: 'prompts/master_script_v1.txt',
  llm: {
    default_preset: 'max',
    presets: {
      max:      { script: 'claude-opus-4-6',          metadata: 'gpt-4.1-mini', thumbnail: 'gpt-4.1' },
      high:     { script: 'claude-sonnet-4-5-20250929', metadata: 'gpt-4.1-mini', thumbnail: 'gpt-4.1' },
      balanced: { script: 'gpt-5.2',                  metadata: 'gpt-4.1-nano', thumbnail: 'gemini-2.5-flash' },
      bulk:     { script: 'deepseek-v3.1',             metadata: 'gpt-4.1-nano', thumbnail: 'gemini-2.5-flash' },
      test:     { script: 'mistral-small-latest',      metadata: 'gemma-3n-e4b-it', thumbnail: 'gemini-2.5-flash' },
    },
  },
  tts:    { provider: 'voiceapi', fallback: 'tts-1-hd' },
  images: { provider: 'wavespeed', fallback: 'gpt-image-1.5' },
  transcriber_output_dir: '${TRANSCRIBER_OUTPUT_DIR}',
}

interface NewChannelFormProps {
  onClose: () => void
  onSaved: () => void
}

function NewChannelForm({ onClose, onSaved }: NewChannelFormProps) {
  const [name, setName] = useState('')
  const [raw, setRaw]   = useState(JSON.stringify(CHANNEL_TEMPLATE, null, 2))
  const [jsonError, setJsonError] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  function handleRaw(val: string) {
    setRaw(val)
    try { JSON.parse(val); setJsonError('') }
    catch { setJsonError('Invalid JSON') }
  }

  async function handleCreate() {
    if (!name.trim()) { setError('Name is required'); return }
    if (jsonError) return
    try {
      const data = JSON.parse(raw)
      setSaving(true)
      await api.channels.save(name.trim(), data)
      onSaved()
      onClose()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">New channel</h3>
        <button onClick={onClose} className="text-gray-500 hover:text-white text-xs">✕</button>
      </div>
      <label className="space-y-1 block">
        <span className="text-xs text-gray-400">File name (e.g. "tech" → tech.json)</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value.replace(/[^\w\-]/g, ''))}
          placeholder="my_channel"
          className="w-full bg-gray-950 border border-gray-600 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
        />
      </label>
      <textarea
        value={raw}
        onChange={(e) => handleRaw(e.target.value)}
        rows={20}
        className="w-full bg-gray-950 border border-gray-600 rounded px-3 py-2 text-xs font-mono text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
        spellCheck={false}
      />
      {jsonError && <p className="text-xs text-red-400">{jsonError}</p>}
      {error    && <p className="text-xs text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button
          onClick={handleCreate}
          disabled={saving || !!jsonError || !name.trim()}
          className="bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white text-xs font-medium px-4 py-1.5 rounded"
        >
          {saving ? 'Creating…' : 'Create'}
        </button>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xs px-3 py-1.5">
          Cancel
        </button>
      </div>
    </div>
  )
}

// ── Prompt editor ─────────────────────────────────────────────────────────────

interface PromptEditorProps {
  name: string
  onClose: () => void
  onSaved: () => void
}

function PromptEditor({ name, onClose, onSaved }: PromptEditorProps) {
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [loading, setLoading]  = useState(true)
  const [saving, setSaving]    = useState(false)
  const [error, setError]      = useState('')

  useEffect(() => {
    api.prompts.get(name)
      .then((d) => { setContent(d.content); setFilename(d.filename); setLoading(false) })
      .catch((e) => { setError(String(e)); setLoading(false) })
  }, [name])

  async function handleSave() {
    try {
      setSaving(true)
      await api.prompts.save(name, content, filename)
      onSaved()
      onClose()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="text-gray-400 text-sm p-4">Loading…</div>
  if (error)   return <div className="text-red-400 text-sm p-4">{error}</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">{filename}</h3>
        <button onClick={onClose} className="text-gray-500 hover:text-white text-xs">✕ close</button>
      </div>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={28}
        className="w-full bg-gray-950 border border-gray-600 rounded px-3 py-2 text-xs font-mono text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
        spellCheck={false}
      />
      <div className="text-xs text-gray-500">{content.length.toLocaleString()} chars</div>
      <div className="flex gap-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs font-medium px-4 py-1.5 rounded"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xs px-3 py-1.5">
          Cancel
        </button>
      </div>
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────

type View =
  | { kind: 'list' }
  | { kind: 'edit_channel'; name: string }
  | { kind: 'new_channel' }
  | { kind: 'edit_prompt'; name: string }

export function ChannelsPanel() {
  const [channels, setChannels] = useState<ChannelMeta[]>([])
  const [prompts,  setPrompts]  = useState<PromptMeta[]>([])
  const [view,     setView]     = useState<View>({ kind: 'list' })
  const [error,    setError]    = useState('')
  const [deleting, setDeleting] = useState<string | null>(null)

  async function load() {
    try {
      const [ch, pr] = await Promise.all([api.channels.list(), api.prompts.list()])
      setChannels(ch)
      setPrompts(pr)
    } catch (e) {
      setError(String(e))
    }
  }

  useEffect(() => { load() }, [])

  async function handleDelete(name: string) {
    if (!confirm(`Delete channel "${name}"?`)) return
    try {
      setDeleting(name)
      await api.channels.delete(name)
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setDeleting(null)
    }
  }

  // ── editors ──

  if (view.kind === 'edit_channel') {
    return (
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <ChannelEditor
          name={view.name}
          onClose={() => setView({ kind: 'list' })}
          onSaved={load}
        />
      </div>
    )
  }

  if (view.kind === 'new_channel') {
    return (
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <NewChannelForm
          onClose={() => setView({ kind: 'list' })}
          onSaved={load}
        />
      </div>
    )
  }

  if (view.kind === 'edit_prompt') {
    return (
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <PromptEditor
          name={view.name}
          onClose={() => setView({ kind: 'list' })}
          onSaved={load}
        />
      </div>
    )
  }

  // ── list view ──

  return (
    <div className="space-y-6">
      {error && (
        <div className="text-xs text-red-300 bg-red-950 rounded p-2">{error}</div>
      )}

      {/* Channels */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-white">
            Channels
            <span className="ml-2 text-gray-500 font-normal text-xs">config/channels/</span>
          </h2>
          <button
            onClick={() => setView({ kind: 'new_channel' })}
            className="bg-green-700 hover:bg-green-600 text-white text-xs font-medium px-3 py-1 rounded"
          >
            + New
          </button>
        </div>

        {channels.length === 0 ? (
          <p className="text-gray-500 text-sm">No channel configs found.</p>
        ) : (
          <div className="divide-y divide-gray-700">
            {channels.map((ch) => (
              <div key={ch.name} className="flex items-center justify-between py-2.5">
                <div>
                  <span className="text-sm text-white font-medium">{ch.channel_name}</span>
                  <span className="ml-2 text-xs text-gray-500">{ch.name}.json</span>
                  <div className="text-xs text-gray-500 mt-0.5">
                    niche: {ch.niche} · lang: {ch.language}
                  </div>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setView({ kind: 'edit_channel', name: ch.name })}
                    className="text-xs text-blue-400 hover:text-blue-300 px-2 py-1 rounded hover:bg-gray-700"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => handleDelete(ch.name)}
                    disabled={deleting === ch.name}
                    className="text-xs text-red-400 hover:text-red-300 px-2 py-1 rounded hover:bg-gray-700 disabled:opacity-50"
                  >
                    {deleting === ch.name ? '…' : 'Delete'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Prompts */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-white">
            Prompts
            <span className="ml-2 text-gray-500 font-normal text-xs">prompts/</span>
          </h2>
        </div>

        {prompts.length === 0 ? (
          <p className="text-gray-500 text-sm">No prompt files found.</p>
        ) : (
          <div className="divide-y divide-gray-700">
            {prompts.map((pr) => (
              <div key={pr.name} className="flex items-center justify-between py-2.5">
                <div>
                  <span className="text-sm text-white font-medium">{pr.filename}</span>
                  <span className="ml-2 text-xs text-gray-500">{fmt(pr.size_bytes)}</span>
                </div>
                <button
                  onClick={() => setView({ kind: 'edit_prompt', name: pr.name })}
                  className="text-xs text-blue-400 hover:text-blue-300 px-2 py-1 rounded hover:bg-gray-700"
                >
                  Edit
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
