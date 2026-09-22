export interface AgentContainer {
  user_id: string
  name: string
  state: string
  started_at?: string
  last_active?: number | null
  active_ws?: number
}

export interface UserView {
  user_id: string
  display_name: string
  slug: string
  gateway_url: string
  /** 仅创建/轮换响应里携带；用户清单不回显 */
  token?: string
  token_version: number
  /** 空闲回收策略（分钟）：null=跟随全局；0=永不回收；正数=自定义上限 */
  idle_timeout_minutes: number | null
  container: AgentContainer | null
  idle_seconds: number | null
}

export interface Overview {
  users: number
  containers_total: number
  containers_running: number
  active_ws: number
  image: string
  network: string
  idle_timeout_minutes: number
  public_url: string
  public_path: string
  default_user: string
  session_ttl_seconds: number
}

export interface LogsResult {
  user_id: string
  tail: number
  logs: string
}
