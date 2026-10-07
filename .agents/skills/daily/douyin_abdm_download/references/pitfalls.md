# 抖音批量下载踩坑实录（2026-10-07 实战验证）

> SKILL.md 只放流程；这里放「为什么」和失败模式，排障时按需读。

## 1. ABDM 插件机制

- 插件 ID `bbobopahenonfdgjgaleledndnnfhooj`（AB Download Manager Browser Integration v1.5.0），**9222 调试 profile（`<project_root>\chrome_debug`）里本来就装了**，无需动主浏览器。同 profile 还有猫抓（catCatch）可做媒体嗅探备选。
- 插件不拦「打开页面」，拦的是**页内点击**：content script 在页面左上角注入横幅「Download With ABDM (N)」，N 是嗅探到的媒体数（会随页面请求增长）。
  - 点横幅头 `.abdm-media-header` = 展开/收起列表
  - 点条目 `.abdm-media-item` = 把该媒体发给 ABDM 下载
  - 源码: `chrome_debug/Default/Extensions/<ID>/1.5.0_0/assets/src/contentscripts/ContentScript-fd951bed.js`
- ABDM 主程序与插件通过本地端口 **15151** 通信；下载目录以 ABDM 设置为准（本机 = `<data_drive>:/<system_data_root><data_drive>:/Downloads`），**别全盘 find**。

## 2. 静默配置

- 存储键: `chrome.storage.local` 的 `silentAddDownload` / `silentStartDownload`。后台每次捕获媒体时**实时读取**（见 `assets/src/background/background-a825e0cc.js`）。
- ⛔ 在设置页用合成事件点 checkbox **不生效**：React 只认真实点击派发的 click，合成 `change`/`input` 不会触发持久化，存储读回来是空的（表现为"点了开关但照样弹窗"）。
- ✅ 治法: 在扩展页上下文里直接 `chrome.storage.local.set({silentAddDownload:true, silentStartDownload:true})`（脚本 `ensure_silent()` 已实现）。注意 CDP evaluate 要 `awaitPromise: True`。
- 静默模式下 ABDM **不再弹「添加下载」确认框**，但仍会为每个下载弹一个 675x240 进度小窗（全部叠在同一位置）→ 批量完用 `close_abdm_popups.py` 清。

## 3. 浏览器生命周期

- ⛔ agent 自己 `Start-Process chrome.exe` 拉起的调试实例**会被回收**（用户原话：你自己开的会被回收）。必须用后端 `exec_python` 跑 `<project_root>\tools\browser\start_debug_browser.py`。
- `start_debug_browser.py` 恢复的窗口可能是 56px 高的扁条 + 崩溃恢复气泡；**脚本用 CDP 按标签页操作，不依赖窗口尺寸**，无需修窗口。若要目检截图，先用 CDP `Page.bringToFront` + `Browser.setWindowBounds`（要显式传 `targetId`）。
- websocket 连 9222 必须带 `suppress_origin=True`，否则 403。

## 4. 抖音页面

- 分享短链 `v.douyin.com/xxx` 打开后 302 到 `www.douyin.com/video/<id>`；未登录有登录墙，但**媒体嗅探不受影响**，横幅照样出现。
- 视频直链（`v11-weba.douyinvod.com`...）带签名有时效，**不要**试图提前抓直链存着，走插件实时嗅探即可。
- 多个媒体条目时选「含 mp4 的体积最大条目」（页面可能嗅探到封面图/其他流）。
- ABDM 落盘文件名 = 页面标题，可能带一大串 `◆play up_xxx ◆model_xxx` 垃圾（作者用了混剪模板）→ 用分享文本里的 `标题【作者】` 自行改名，别依赖页面标题。

## 5. 下载完成判定

- ABDM 分段下载，**最后才合成完整 mp4**；判定 = 目录里出现新 `.mp4` 且体积 4 秒无增长且 >100KB。逐条串行处理时「新于 since 时间戳的最新 mp4」判据可靠。

## 6. Windows 杂项

- 关弹窗用 python ctypes `PostMessageW(hwnd, WM_CLOSE)`；**Add-Type 会被安全策略拦死**。
- 批量脚本要在浏览器**未被回收**的前提下跑；跑前 `curl 127.0.0.1:9222/json/version` 探活。
