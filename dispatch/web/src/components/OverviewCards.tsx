import type { Overview } from '../types'

export function OverviewCards({ overview: o }: { overview: Overview }) {
  const cards: Array<{ label: string; value: string; hint?: string }> = [
    { label: '用户', value: String(o.users) },
    {
      label: '容器',
      value: `${o.containers_running} / ${o.containers_total}`,
      hint: '运行中 / 总数',
    },
    { label: '活跃 WS', value: String(o.active_ws), hint: 'desktop 长连接' },
    {
      label: '空闲回收',
      value: o.idle_timeout_minutes > 0 ? `${o.idle_timeout_minutes} 分钟` : '关闭',
      hint: o.image,
    },
  ]
  return (
    <section className="cards">
      {cards.map((c) => (
        <div className="card stat" key={c.label}>
          <div className="stat-label">{c.label}</div>
          <div className="stat-value">{c.value}</div>
          {c.hint && <div className="stat-hint muted">{c.hint}</div>}
        </div>
      ))}
    </section>
  )
}
