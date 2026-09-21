import { useState } from 'react'

export function Login({ onLogin }: { onLogin: (key: string) => void }) {
  const [key, setKey] = useState('')
  return (
    <div className="card login-card">
      <h1>
        hermes-dispatch <span className="muted">管理台</span>
      </h1>
      <p className="muted">输入管理 Key（HERMES_ADMIN_KEY）登录</p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (key.trim()) onLogin(key.trim())
        }}
      >
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="admin key"
          autoFocus
        />
        <button className="btn primary" type="submit" disabled={!key.trim()}>
          登录
        </button>
      </form>
    </div>
  )
}
