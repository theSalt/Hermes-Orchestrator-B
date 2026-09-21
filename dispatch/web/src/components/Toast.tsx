export function Toast({ toast }: { toast: { kind: 'ok' | 'err'; text: string } | null }) {
  if (!toast) return null
  return <div className={`toast ${toast.kind}`}>{toast.text}</div>
}
