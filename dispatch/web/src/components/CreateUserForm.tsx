import { useState } from 'react'

const ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/

export function CreateUserForm({
  busy,
  onCreate,
}: {
  busy: boolean
  onCreate: (user_id: string, display_name: string) => void
}) {
  const [uid, setUid] = useState('')
  const [name, setName] = useState('')
  const valid = ID_RE.test(uid)
  return (
    <section className="card">
      <h3>添加用户</h3>
      <form
        className="form-row"
        onSubmit={(e) => {
          e.preventDefault()
          if (valid) {
            onCreate(uid.trim(), name.trim())
            setUid('')
            setName('')
          }
        }}
      >
        <input
          value={uid}
          onChange={(e) => setUid(e.target.value)}
          placeholder="用户 ID（如 alice）"
          pattern="[a-z0-9][a-z0-9._-]{0,63}"
          title="小写字母/数字开头，可含 . _ -，≤64 字符"
          required
        />
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="显示名（可选）"
        />
        <button className="btn primary" type="submit" disabled={!valid || busy}>
          创建
        </button>
      </form>
      <p className="muted small">
        创建是惰性的：不立即拉容器，用户首次从 desktop 连接时按需冷启动。
      </p>
    </section>
  )
}
