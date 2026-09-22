import { api } from '../api'
import type { UserView } from '../types'
import { CopyBtn } from './CopyBtn'

type Act = (fn: () => Promise<string | null | void>, fallbackOk?: string) => Promise<void>

interface Props {
  users: UserView[]
  busy: boolean
  notify: (kind: 'ok' | 'err', text: string) => void
  act: Act
  onLogs: (uid: string) => void
  onRotated: (u: UserView, result: { token: string; token_version: number; gateway_url: string; note: string }) => void
}

function fmtIdle(sec: number | null): string {
  if (sec == null) return '—'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`
  return `${Math.floor(sec / 86400)}d`
}

function stateBadge(u: UserView) {
  const state = u.container?.state ?? 'absent'
  const cls =
    state === 'running' ? 'ok' : state === 'absent' ? 'dim' : state === 'exited' ? 'warn' : 'err'
  const ws = u.container?.active_ws ?? 0
  return (
    <span className={`badge ${cls}`} title={`idle ${fmtIdle(u.idle_seconds)} · WS ${ws}`}>
      {state}
      {state === 'running' && ws > 0 ? ` ·ws${ws}` : ''}
    </span>
  )
}

/** 空闲回收策略展示文案；与后端 set_idle_timeout 语义对齐 */
function policyLabel(minutes: number | null): string {
  if (minutes == null) return '跟随全局'
  if (minutes === 0) return '永不回收'
  return `${minutes}m 上限`
}

export function UserTable({ users, busy, act, notify, onLogs, onRotated }: Props) {
  function rotate(u: UserView) {
    if (
      !confirm(
        `轮换用户 ${u.user_id} 的 token？\n` +
          '· 旧 token 立即失效，desktop 需更新 token 后重连\n' +
          '· 容器将删除重建（数据保留）',
      )
    )
      return
    act(async () => {
      const res = await api.rotateToken(u.user_id)
      onRotated(u, res)
      return null // 面板承担提示
    })
  }

  function del(u: UserView) {
    if (!confirm(`删除用户 ${u.user_id}？容器与注册记录将被删除。`)) return
    const purge = confirm(
      '是否同时清除该用户的数据目录（记忆/会话/文件，不可恢复）？\n确定 = 清除，取消 = 保留数据',
    )
    act(
      () =>
        api.deleteUser(u.user_id, purge).then(
          () => `用户 ${u.user_id} 已删除（数据${purge ? '已清除' : '保留'}）`,
        ),
    )
  }

  function setIdle(u: UserView) {
    const input = prompt(
      `用户 ${u.user_id} 的空闲回收策略（分钟）：\n` +
        '· 留空 = 跟随全局默认\n' +
        '· 0 = 永不回收（messaging 重度用户推荐）\n' +
        '· 正整数 = 自定义空闲上限',
      u.idle_timeout_minutes == null ? '' : String(u.idle_timeout_minutes),
    )
    if (input === null) return
    const trimmed = input.trim()
    let minutes: number | null = null
    if (trimmed !== '') {
      const n = Number(trimmed)
      if (!Number.isInteger(n) || n < 0) {
        notify('err', '空闲策略须为空（跟随全局）、0（永不回收）或正整数分钟')
        return
      }
      minutes = n
    }
    act(async () => {
      const res = await api.setIdleTimeout(u.user_id, minutes)
      return `空闲策略已更新：${policyLabel(res.idle_timeout_minutes)}（当前生效 ${res.effective_minutes} 分钟）`
    })
  }

  return (
    <section className="card">
      <h3>用户（{users.length}）</h3>
      {users.length === 0 ? (
        <p className="muted">暂无用户，用上方表单创建。</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>用户</th>
                <th>容器</th>
                <th>空闲</th>
                <th>token (v)</th>
                <th>gateway</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_id}>
                  <td>
                    <div className="uid">{u.user_id}</div>
                    {u.display_name && <div className="muted small">{u.display_name}</div>}
                  </td>
                  <td>{stateBadge(u)}</td>
                  <td>
                    {fmtIdle(u.idle_seconds)}
                    <div className="muted small" title="空闲回收策略：null=跟随全局；0=永不回收；正数=自定义分钟上限">
                      {policyLabel(u.idle_timeout_minutes)}
                    </div>
                  </td>
                  <td>
                    {/* token 仅创建/轮换时一次性展示，这里不再回显（遗忘走轮换） */}
                    <span className="muted" title="token 仅创建/轮换时一次性展示；遗忘请轮换">
                      v{u.token_version}
                    </span>
                  </td>
                  <td>
                    <CopyBtn text={u.gateway_url} label="复制 URL" />
                  </td>
                  <td className="actions">
                    {u.container?.state === 'running' ? (
                      <button
                        className="btn small"
                        disabled={busy}
                        onClick={() =>
                          act(() => api.stop(u.user_id).then(() => `${u.user_id} 已停止`))
                        }
                      >
                        停止
                      </button>
                    ) : (
                      <button
                        className="btn small"
                        disabled={busy}
                        onClick={() =>
                          act(() => api.start(u.user_id).then(() => `${u.user_id} 已就绪`))
                        }
                      >
                        启动
                      </button>
                    )}
                    <button
                      className="btn small"
                      disabled={busy}
                      onClick={() =>
                        act(() => api.restart(u.user_id).then(() => `${u.user_id} 重启完成`))
                      }
                    >
                      重启
                    </button>
                    <button className="btn small" disabled={busy} onClick={() => onLogs(u.user_id)}>
                      日志
                    </button>
                    <button className="btn small" disabled={busy} onClick={() => setIdle(u)}>
                      空闲策略
                    </button>
                    <button className="btn small warn" disabled={busy} onClick={() => rotate(u)}>
                      轮换 token
                    </button>
                    <button className="btn small danger" disabled={busy} onClick={() => del(u)}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
