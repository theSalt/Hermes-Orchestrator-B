import type { UserView } from '../types'
import { CopyBtn } from './CopyBtn'

/** 一次性凭据面板：新建用户 / 轮换 token 后展示 token 与 gateway_url。
 * 仅在这两个时机渲染，token 必然存在。 */
export function CredentialPanel({
  panel,
  onClose,
}: {
  panel: { title: string; note?: string; user: UserView & { token: string } }
  onClose: () => void
}) {
  const { user: u } = panel
  return (
    <div className="overlay" onClick={onClose}>
      <div className="card panel" onClick={(e) => e.stopPropagation()}>
        <h3>{panel.title}</h3>
        {panel.note && <p className="muted">{panel.note}</p>}
        <div className="field">
          <label>Gateway URL（desktop → Remote URL）</label>
          <div className="kv">
            <code>{u.gateway_url}</code>
            <CopyBtn text={u.gateway_url} />
          </div>
        </div>
        <div className="field">
          <label>Token（desktop → Token）</label>
          <div className="kv">
            <code className="wrap">{u.token}</code>
            <CopyBtn text={u.token} />
          </div>
        </div>
        <p className="muted small">
          token 版本 v{u.token_version}；轮换后旧 token 立即失效，desktop 需更新后重连。
        </p>
        <div className="row-end">
          <button className="btn primary" onClick={onClose}>
            我已保存，关闭
          </button>
        </div>
      </div>
    </div>
  )
}
