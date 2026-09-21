import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from './api'
import type { UserView } from './types'
import { CredentialPanel } from './components/CredentialPanel'
import { CreateUserForm } from './components/CreateUserForm'
import { LogsModal } from './components/LogsModal'
import { Login } from './components/Login'
import { OverviewCards } from './components/OverviewCards'
import { Toast } from './components/Toast'
import { UserTable } from './components/UserTable'

type AuthState = 'checking' | 'anonymous' | 'authed'
// 面板只在创建/轮换成功后渲染，token 必然存在
type Panel = { title: string; note?: string; user: UserView & { token: string } }

export default function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [overview, setOverview] = useState<Awaited<ReturnType<typeof api.overview>> | null>(null)
  const [users, setUsers] = useState<UserView[]>([])
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [panel, setPanel] = useState<Panel | null>(null)
  const [logsFor, setLogsFor] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [busy, setBusy] = useState(false)
  const toastTimer = useRef<number | undefined>(undefined)

  const notify = useCallback((kind: 'ok' | 'err', text: string) => {
    setToast({ kind, text })
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 5000)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [ov, us] = await Promise.all([api.overview(), api.users()])
      setOverview(ov)
      setUsers(us.users)
      setAuth('authed')
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setAuth('anonymous')
        return
      }
      notify('err', `刷新失败：${(e as Error).message}`)
    }
  }, [notify])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    if (!autoRefresh || auth !== 'authed') return
    const t = window.setInterval(refresh, 15000)
    return () => window.clearInterval(t)
  }, [autoRefresh, auth, refresh])

  /** 执行一个管理操作：统一 busy/401/错误处理，成功后刷新列表。
   * fn 返回 null 表示提示由面板承担，不弹 toast。 */
  const act = useCallback(
    async (fn: () => Promise<string | null | void>, fallbackOk = '操作完成') => {
      if (busy) return
      setBusy(true)
      try {
        const msg = await fn()
        if (msg !== null) notify('ok', msg || fallbackOk)
        await refresh()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) setAuth('anonymous')
        else notify('err', (e as Error).message)
      } finally {
        setBusy(false)
      }
    },
    [busy, notify, refresh],
  )

  if (auth === 'checking') {
    return (
      <div className="page">
        <p className="muted center">连接 dispatch …</p>
      </div>
    )
  }

  if (auth === 'anonymous') {
    return (
      <div className="page narrow">
        <Login
          onLogin={async (key) => {
            try {
              await api.login(key)
              notify('ok', '登录成功')
              await refresh()
            } catch (e) {
              notify('err', (e as Error).message)
            }
          }}
        />
        <Toast toast={toast} />
      </div>
    )
  }

  return (
    <div className="page">
      <header className="topbar">
        <h1>
          hermes-dispatch <span className="muted">管理台</span>
        </h1>
        <div className="topbar-actions">
          <label className="chk">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            15s 自动刷新
          </label>
          <button className="btn" onClick={() => refresh()} disabled={busy}>
            刷新
          </button>
          <button
            className="btn"
            disabled={busy}
            onClick={() =>
              act(async () => {
                await api.logout()
                setAuth('anonymous')
                return '已登出'
              })
            }
          >
            登出
          </button>
        </div>
      </header>

      {overview && <OverviewCards overview={overview} />}

      <CreateUserForm
        busy={busy}
        onCreate={(uid, name) =>
          act(async () => {
            const u = await api.createUser(uid, name)
            setPanel({ title: `用户 ${uid} 已创建`, note: '请把以下凭据交付给用户', user: u })
            return null // 面板已承担成功提示
          }, '用户已创建')
        }
      />

      <UserTable
        users={users}
        busy={busy}
        notify={notify}
        act={act}
        onLogs={(uid) => setLogsFor(uid)}
        onRotated={(u, result) =>
          setPanel({
            title: `用户 ${u.user_id} 的 token 已轮换（v${result.token_version}）`,
            note: result.note,
            user: { ...u, token: result.token, token_version: result.token_version, gateway_url: result.gateway_url },
          })
        }
      />

      {panel && <CredentialPanel panel={panel} onClose={() => setPanel(null)} />}
      {logsFor && <LogsModal userId={logsFor} onClose={() => setLogsFor(null)} />}
      <Toast toast={toast} />
    </div>
  )
}
