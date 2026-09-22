/** 插件入口（IIFE，由宿主以 <script src=/dashboard-plugins/hermes-attachments/dist/index.js> 注入）。
 * SDK/注册面缺失（宿主过旧或非 dashboard 页面）时仅告警并禁用自身，不污染宿主。
 * 注册窗口：bundle 同步执行期间注册即可（上游 2s 初始窗口覆盖异步加载场景）。
 */
import { getRegistry, getSdk } from './sdk'
import { injectStyles } from './styles'
import { Toolbar } from './Toolbar'
import { Drawer } from './Drawer'

const sdk = getSdk()
const registry = getRegistry()

if (!sdk || !registry || !sdk.api?.authedFetch || !sdk.api?.buildWsUrl) {
  console.warn('[hermes-attachments] 插件 SDK 未就绪，插件保持禁用')
} else {
  injectStyles()
  registry.registerSlot('hermes-attachments', 'chat:bottom', Toolbar)
  registry.registerSlot('hermes-attachments', 'overlay', Drawer)
}
