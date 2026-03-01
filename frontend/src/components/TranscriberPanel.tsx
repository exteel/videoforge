/**
 * TranscriberPanel — Інтеграція з Transcriber.
 *
 * Потік:
 *  1. Відкрити Transcriber GUI (кнопка Launch)
 *  2. У Transcriber вставити YouTube URL і запустити
 *  3. Тут — натиснути Scan і обрати готову директорію
 *  4. Клік "▶ Run Pipeline" → підставляє шлях і запускає пайплайн
 */

import { useEffect, useState } from 'react'
import { api, type TranscriberOutput, type TranscriberStatus } from '../api'

interface Props {
  /** Викликається коли користувач обирає output dir → підставити в форму Jobs */
  onSelectDir: (dir: string) => void
}

function fmtDate(ts: number): string {
  try { return new Date(ts * 1000).toLocaleString('uk-UA', { dateStyle: 'short', timeStyle: 'short' }) }
  catch { return '—' }
}

export function TranscriberPanel({ onSelectDir }: Props) {
  const [status, setStatus]     = useState<TranscriberStatus | null>(null)
  const [outputs, setOutputs]   = useState<TranscriberOutput[]>([])
  const [scanning, setScanning] = useState(false)
  const [launching, setLaunching] = useState(false)
  const [launchMsg, setLaunchMsg] = useState('')
  const [scanTime, setScanTime]   = useState(0)   // timestamp of last scan

  async function loadStatus() {
    try { setStatus(await api.transcriber.status()) } catch { /* ignore */ }
  }

  async function scan() {
    setScanning(true)
    try {
      const res = await api.transcriber.outputs()
      setOutputs(res)
      setScanTime(Date.now() / 1000)
    } finally {
      setScanning(false)
    }
  }

  async function handleLaunch() {
    setLaunching(true)
    setLaunchMsg('')
    try {
      const res = await api.transcriber.launch()
      setLaunchMsg(res.message)
    } catch (e) {
      setLaunchMsg(String(e))
    } finally {
      setLaunching(false)
    }
  }

  useEffect(() => {
    loadStatus()
    scan()
  }, [])

  const notFound = status && !status.transcriber_found

  return (
    <div className="bg-gray-800 rounded-lg border border-gray-700 p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <div className="text-sm font-semibold text-white">Transcriber</div>
          <div className="text-xs text-gray-400 mt-0.5">
            {status === null
              ? 'Перевірка…'
              : status.transcriber_found
              ? `✅ Знайдено · ${status.outputs_count} готових виходів`
              : `❌ Не знайдено: ${status.transcriber_path}`}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <button
            onClick={handleLaunch}
            disabled={launching || notFound === true}
            className="text-xs px-3 py-1.5 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 text-white font-medium"
          >
            {launching ? '…' : '🚀 Відкрити Transcriber'}
          </button>
          <button
            onClick={scan}
            disabled={scanning}
            className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-gray-300"
          >
            {scanning ? '⏳ Сканування…' : '↻ Scan'}
          </button>
        </div>
      </div>

      {/* Launch message */}
      {launchMsg && (
        <div className={`text-xs rounded p-2 ${launchMsg.includes('Error') || launchMsg.includes('Failed') ? 'bg-red-950 text-red-300' : 'bg-indigo-950 text-indigo-300'}`}>
          {launchMsg}
        </div>
      )}

      {/* Not found warning */}
      {notFound && (
        <div className="text-xs bg-yellow-950 text-yellow-300 rounded p-3 space-y-1">
          <div className="font-semibold">Transcriber не знайдено</div>
          <div>Додай в <code>.env</code>:</div>
          <code className="block">TRANSCRIBER_PY=D:/transscript batch/Transcriber/transcriber.py</code>
          <code className="block">TRANSCRIBER_OUTPUT=D:/transscript batch/output/output</code>
        </div>
      )}

      {/* Instructions */}
      <div className="text-xs text-gray-500 space-y-1">
        <div className="font-medium text-gray-400">Як використовувати:</div>
        <ol className="list-decimal list-inside space-y-0.5 ml-1">
          <li>Натисни <strong>Відкрити Transcriber</strong> → вставити YouTube URL → запустити</li>
          <li>Дочекатись завершення транскрипції (кілька хвилин)</li>
          <li>Натисни <strong>Scan</strong> → обери готовий вихід нижче</li>
          <li>Клік <strong>▶ Pipeline</strong> → підставить шлях у форму і запустить</li>
        </ol>
      </div>

      {/* Output list */}
      {outputs.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-medium text-gray-400 uppercase tracking-wide">
            Готові виходи ({outputs.length})
          </div>
          {outputs.map((o) => (
            <div
              key={o.dir}
              className="flex items-center justify-between gap-2 bg-gray-900 rounded p-3 border border-gray-700 hover:border-indigo-600 transition-colors"
            >
              <div className="min-w-0">
                <div className="text-sm text-white truncate">{o.title || o.name}</div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {o.language && <span className="mr-2">{o.language}</span>}
                  {o.has_srt && <span className="mr-2">📄 SRT</span>}
                  {o.has_description && <span className="mr-2">📝 desc</span>}
                  {o.has_thumbnail && <span className="mr-2">🖼 thumb</span>}
                  <span className="text-gray-600">{fmtDate(o.modified_at)}</span>
                </div>
                <div className="text-xs text-gray-600 truncate mt-0.5">{o.dir}</div>
              </div>
              <button
                onClick={() => onSelectDir(o.dir)}
                className="shrink-0 text-xs px-3 py-1.5 rounded bg-blue-700 hover:bg-blue-600 text-white font-medium"
              >
                ▶ Pipeline
              </button>
            </div>
          ))}
        </div>
      )}

      {outputs.length === 0 && !scanning && (
        <div className="text-xs text-gray-500 text-center py-2">
          {status?.output_dir_exists
            ? 'Готових виходів не знайдено. Запусти Transcriber і натисни Scan.'
            : `Вихідна папка не існує: ${status?.output_dir ?? '…'}`}
        </div>
      )}
    </div>
  )
}
