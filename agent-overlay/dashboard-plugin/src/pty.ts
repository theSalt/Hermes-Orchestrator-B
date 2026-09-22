/** 把文本预填进 chat TUI 的输入框（不代发）。
 *
 * chat 页终端是 xterm over PTY WS（/api/pty，纯文本帧，非 JSON）——上游图片上传
 * （/image <path> + \r）与 /copy 都是同款字节注入。本插件只发文本、不发 \r，
 * 文本即停留在 TUI 输入框内，由用户自行补指令后回车。
 * 预填文本以「[附件] 」开头，避免以 / 开头被 TUI 误判为 slash 命令。
 */
import { getSdk } from './sdk'

export async function prefill(text: string): Promise<void> {
  const sdk = getSdk()
  if (!sdk) throw new Error('插件 SDK 不可用')
  const url = await sdk.api.buildWsUrl('/api/pty')
  const ws = new WebSocket(url)
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('pty 连接超时')), 8000)
      ws.onopen = () => {
        clearTimeout(timer)
        resolve()
      }
      ws.onerror = () => {
        clearTimeout(timer)
        reject(new Error('pty 连接失败'))
      }
    })
    ws.send(text) // 关键：无 \r / \n —— 只预填，不提交
    // 留出字节冲刷时间再关连接，避免尾帧被丢弃
    await new Promise((resolve) => setTimeout(resolve, 200))
  } finally {
    try {
      ws.close()
    } catch {
      /* noop */
    }
  }
}
