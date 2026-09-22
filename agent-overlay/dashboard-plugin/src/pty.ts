/** 把文本预填进 chat TUI 的输入框（不代发）。
 *
 * chat 终端是 xterm over PTY WS（/api/pty）。不能另开 WS 连接写 PTY：
 * 上游 keep-alive 会话只允许一个挂接 socket，插件的第二条连接会把自己页面
 * 那条顶掉（表现为 Chat disconnected，踩过）。
 * 正确路径：向 xterm 的隐藏 textarea 派发合成 paste 事件——与 Ctrl+V 同一条
 * 处理链路（xterm onPaste → 写入 PTY），走页面自己那条连接，文本停留输入框。
 * 前缀「[附件] 」避免整体被误读为 slash 命令。
 */

export async function prefill(text: string): Promise<void> {
  const ta = document.querySelector<HTMLTextAreaElement>('.xterm-helper-textarea')
  if (!ta) throw new Error('找不到 chat 终端输入区（请先进入 Chat 页）')
  const dt = new DataTransfer()
  dt.setData('text/plain', text)
  const ev = new ClipboardEvent('paste', {
    clipboardData: dt,
    bubbles: true,
    cancelable: true,
  })
  ta.dispatchEvent(ev)
  // 不确定宿主是否异步消费：留给 xterm 一拍处理时间
  await new Promise((resolve) => setTimeout(resolve, 80))
}
