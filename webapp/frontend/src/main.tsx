import React from 'react'
import { createRoot } from 'react-dom/client'
import * as Tooltip from '@radix-ui/react-tooltip'
import App from './App'
import './styles.css'

// 字体自托管 (@fontsource), 不走 Google Fonts <link>:
// 桌面壳离线也要能起, 且首帧不该等外网。中文交给系统字体, 不内嵌几 MB 的 CJK webfont。
//
// 这里刻意**不**预设 data-theme: 首帧交给 CSS 按 prefers-color-scheme 解析。
// App 挂载后会把它写成具体的 dark|light (值和 CSS 首帧解析结果一致, 不会翻转),
// 因为界面上只剩暗/亮两档, 不再有"跟随系统"这一档。
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Tooltip.Provider delayDuration={280}>
      <App />
    </Tooltip.Provider>
  </React.StrictMode>,
)
