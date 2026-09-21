import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

const TAIL = 300

export function LogsModal({ userId, onClose }: { userId: string; onClose: () => void }) {
  const [logs, setLogs] = useState<string>('')
  const [empty, setEmpty] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [auto, setAuto] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.logs(userId, TAIL)
      setLogs(r.logs)
      setEmpty(r.logs.length === 0)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [userId])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!auto) return
    const t = window.setInterval(load, 30000)
    return () => window.clearInterval(t)
  }, [auto, load])

  return (
    <div className="overlay" onClick={onClose}>
      <div className="card panel logs-panel" onClick={(e) => e.stopPropagation()}>
        <div className="row-between">
          <h3>容器日志 · {userId}（最近 {TAIL} 行）</h3>
          <div className="topbar-actions">
            <label className="chk">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              30s 刷新
            </label>
            <button className="btn small" onClick={load} disabled={loading}>
              刷新
            </button>
            <button className="btn small" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        {err ? (
          <p className="err-text">{err}</p>
        ) : empty && !loading ? (
          <p className="muted">暂无日志（容器不存在或尚未输出）。</p>
        ) : (
          <pre className="logs">
            {logs}
            {loading && '\n加载中 …'}
          </pre>
        )}
      </div>
    </div>
  )
}
