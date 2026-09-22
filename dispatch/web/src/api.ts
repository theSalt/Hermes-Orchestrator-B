import type { LogsResult, Overview, UserView } from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// 页面挂在 /admin/ 下（直连或 nginx subpath），相对路径请求自动落在
// /admin/api/*；vite dev 模式下由 proxy 转发。两种部署形态都无需配置。
async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    })
  } catch {
    throw new ApiError(0, '网络错误：无法连接 dispatch')
  }
  if (res.status === 401) {
    throw new ApiError(401, '未登录或会话已过期')
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') detail = body.detail
      else if (body) detail = JSON.stringify(body)
    } catch {
      /* 非 JSON 响应，保留状态码信息 */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

function body(payload: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(payload) }
}

export const api = {
  login: (key: string) => req<{ status: string }>('api/login', body({ key })),
  logout: () => req<{ status: string }>('api/logout', { method: 'POST' }),
  overview: () => req<Overview>('api/overview'),
  users: () => req<{ users: UserView[] }>('api/users'),
  createUser: (user_id: string, display_name: string) =>
    // 创建响应是 token 唯一一次返回的时机之一
    req<UserView & { token: string }>('api/users', body({ user_id, display_name })),
  deleteUser: (uid: string, purge: boolean) =>
    req<{ status: string }>(`api/users/${encodeURIComponent(uid)}${purge ? '?purge=1' : ''}`, {
      method: 'DELETE',
    }),
  start: (uid: string) => req<{ status: string }>(`api/users/${encodeURIComponent(uid)}/start`, { method: 'POST' }),
  stop: (uid: string) => req<{ status: string }>(`api/users/${encodeURIComponent(uid)}/stop`, { method: 'POST' }),
  restart: (uid: string) => req<{ status: string }>(`api/users/${encodeURIComponent(uid)}/restart`, { method: 'POST' }),
  rotateToken: (uid: string) =>
    req<{ token: string; token_version: number; gateway_url: string; note: string }>(
      `api/users/${encodeURIComponent(uid)}/token/rotate`,
      { method: 'POST' },
    ),
  setIdleTimeout: (uid: string, minutes: number | null) =>
    req<{ status: string; idle_timeout_minutes: number | null; effective_minutes: number; note: string }>(
      `api/users/${encodeURIComponent(uid)}/idle-timeout`,
      {
        method: 'PUT',
        body: JSON.stringify({ minutes }),
      },
    ),
  logs: (uid: string, tail = 200) =>
    req<LogsResult>(`api/users/${encodeURIComponent(uid)}/logs?tail=${tail}`),
}

/** 复制文本；http 内网直连下 navigator.clipboard 不存在，退回 execCommand。 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fallthrough */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}
