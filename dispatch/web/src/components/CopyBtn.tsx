import { useState } from 'react'
import { copyText } from '../api'

export function CopyBtn({ text, label = '复制' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      className="btn small"
      onClick={async () => {
        const ok = await copyText(text)
        setDone(ok)
        setTimeout(() => setDone(false), 1500)
      }}
    >
      {done ? '已复制 ✓' : label}
    </button>
  )
}
