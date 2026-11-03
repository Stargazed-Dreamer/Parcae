---
aliases: ['xhs', 'xiaohongshu', '小红书', 'rednote', 'xiaohongshu.com']
---

# xiaohongshu.com (xiaohongshu.com)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | xiaohongshu.com |
| **首次接触日期** | 2026-10-12 |
| **相关 skill** | web_archive（extract_xiaohongshu.py） |
| **登录态要求** | 游客态可读正文+部分评论+评论图；**全量评论需登录**；⛔ **脚本有前置门禁：游客态默认拒绝执行** |
| **抓取频次** | 一次性 / 按需（用户给链接） |
| **文件创建日期** | 2026-10-12 |
| **最后更新** | 2026-10-12 |

## 网站概况

小红书（xiaohongshu.com）笔记详情页（`/explore/{note_id}`）存档：抓正文 + 图片组（多图笔记）+ 视频直链 + 评论区（主评论/子回复/评论图）。agent 通常在上面做"把一篇/一批笔记存成 MD（含评论）"的任务。SPA + React，class 为**语义化命名**（非混淆 hash），比小黑盒更干净。

## 登录态来源与限制

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `user-data-dir` |
| 游客态能力 | 正文 + 图片组 + 约 10 条主评论 + 评论图 | — |
| ⛔ **封顶** | 评论滚动**不加载更多**（实测 `共 873 条` 只渲染 10 条，滚 5 次仍 10） | 要全量评论（含"展开 N 条回复"背后的子回复）必须登录 |
| ⛔ **门禁** | `extract_xiaohongshu.py` 启动即探测登录态，**游客态 `[ABORT]` 拒绝执行**（`--allow-unlogged` 才放行） | 2026-10-12 用户裁决；见下「登录态判据」 |

### 登录态判据（2026-10-12 探针实测）

| 信号 | 游客态 | 可靠性 |
|------|--------|--------|
| 顶栏搜索框 `input#search-input` 的 placeholder | 「登录探索更多内容」（登录后为普通搜索提示） | **主判据，即时可见**（3 次探测均复现） |
| 登录弹窗 `.login-modal` + `input[placeholder="输入手机号"/"输入验证码"]` | 延迟 ~6-7s 才自动弹出 | 辅助判据，**须轮询 ~10s 窗口**，不能 6s 就判「已登录」 |
| 评论区 `.comments-login`（"登录后评论"） | 出现 | 辅助 |
| ⛔ cookie `web_session` | **游客态也存在**（设备态，len=38） | **不可作判据** |

> 门禁失败（加载异常/无法确认）一律按未登录处理（fail-safe），与 `extract_web.py` 的小黑盒门禁同口径。

## 反爬/风控

- **页面态**：游客态即可拿正文 + 图片组 + 部分评论，未实测到验证码/滑块（登录弹窗非风控拦截）。
- **拟真建议**：`frontmatter source:` 保留**完整原始分享 URL**（`xsec_token`/`xhsshare`/`app_platform`/`apptime` 等），让访问长得像真实用户从 App 分享点进来；精简参数反而制造非自然 URL。
- **权重提示**：URL 参数**不是**主判据，真正决定成败是 ①登录态 cookie ②浏览器指纹 ③访问节奏 ④Referer/导航方式。带全参数是零成本加分项，不是护身符。
- **图片防盗链**：下载评论图/正文图带 `Referer: https://www.xiaohongshu.com`。

## DOM 结构与提取规则

### 字段提取表

| 字段 | 选择器 | 注意事项 |
|------|--------|---------|
| 标题 | `h1.title` | 正文页唯一；`div.title` 是登录框的须排除；无 h1 时 JSON-LD `name` 去 ` - 小红书` 后缀 |
| 正文 | `#detail-desc` | len 与页面正文一致 |
| 作者 | `#noteContainer .username` | 回退 JSON-LD `author.name` |
| 图片组 | JSON-LD `image[]` | 优先；DOM `.swiper-slide img` / `.note-slider img` 兜底（受虚拟化，勿主用） |
| 视频直链 | JSON-LD `contentUrl` | 视频笔记才有；另有 `thumbnailUrl` 封面 |
| 发布时间 | JSON-LD `uploadDate` / `datePublished` | ISO，转 YYYY-MM-DD |
| 评论总数 | `.comments-container .total` | 文本形如"共 873 条评论" |
| 主评论 | `:scope > .comment-item`（外层 list 下） | 子回复 class 带 `comment-item`，须排除 |
| 子回复 | `.parent-comment > .reply-container > .list-container > .comment-item-sub` | 平铺在内层 list |
| 评论字段 | `.right` 下：`.author-wrapper .name` / `.content .note-text` / `.info .date > span:first-child` / `.info .date .location` / `.info .interactions .like` | 主评论与子回复同构 |
| 评论图 | `.comment-picture img.inner`，src 含 `/comment/` | 表情 `img.note-content-emoji` 不含该串 |

### 页面布局变体

- **视频笔记**：`#noteContainer` 内 `.video-player-media.media-container`；JSON-LD `@type=VideoObject`，`image[]` 为空、有 `contentUrl`。
- **图文笔记**：`.swiper-slide`/`.note-slider` 多图；JSON-LD `@type=Article`（实测），`image[]` 与 DOM slider 一致。

### JSON-LD 元信息（优先走它，少走 DOM）

```javascript
const ld = JSON.parse(document.querySelector('script[type="application/ld+json"]').textContent);
// ld.name / ld.image[] / ld.uploadDate / ld.contentUrl / ld.author.name / ld.interactionStatistic
```

## URL 规则与重定向

- **note_id 在 path**：`/explore/{note_id}`（也见 `/discovery/item/{note_id}`），note_id 为 20+ 位 hex，如 `6acb4c47000000000b0055ab`——**去重/索引 key 用它**。
- **分享 URL 参数**：`xsec_token`（每篇独立生成，可能绑设备/会过期，原样照抄）、`xhsshare`/`app_platform`/`app_version`/`apptime`/`share_id`/`type`/`xsec_source` 等（分享指纹，保留可拟真）。
- **精简与否**：`xsec_token` 实测**有效且笔记可访问**（不带则可能要求登录）；其余可去，但**用户裁决保留完整 URL 拟真**。

## 图片/资源加载

| 项 | 选择器/规则 | 备注 |
|----|-----------|------|
| 懒加载占位符 | 无（`img.src` 直接是 http） | 与小黑盒不同，这里 src 不空 |
| 图片域名 | `sns-webpic-qc.xhscdn.com`（正文/评论图）、`sns-avatar-qc.xhscdn.com`（头像） | 评论图 src 含 `/comment/` |
| 防盗链 | 需 `Referer: https://www.xiaohongshu.com` | 下载时带 |
| 扩展名 | URL path 常**无扩展名** | 按响应 `Content-Type` 判 `.jpg/.webp/.png` |
| 视频 | JSON-LD `contentUrl` 为 mp4 直链（带 sign 参数） | 建议只记 URL，不下载视频 |

## 评论/动态内容

| 项 | 选择器/规则 | 备注 |
|----|-----------|------|
| 评论容器 | `.comments-container` | 内含 `.total` + 外层 `.list-container` |
| 主评论项 | `.comments-container > .list-container > .parent-comment` | 外层 list 直接子 |
| 主评论本体 | `.parent-comment > .comment-item` | ⛔ 子回复也带 `comment-item` class |
| 子回复项 | `.parent-comment > .reply-container > .list-container > .comment-item-sub` | 平铺，非嵌套 |
| 展开机制 | JS `el.click()` 点 `.reply-container > .show-more`（文本"展开 N 条回复"） | Playwright `.click()` 被 `.reds-mask` 拦截 |
| 评论总数 | `.comments-container .total` | "共 N 条评论" |
| 异步加载 | 滚动 `.note-scroller`（`overflow-y:scroll`） | 不是 window |

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|
| 图片组受 swiper 虚拟化只渲染 5 个 slide | 从 DOM `.swiper-slide img` 收集 | 优先取页面内嵌 `script[type="application/ld+json"]` 的 `image` 数组（与 slider 一致、不虚化），DOM 兜底 | 2026-10-12 |
| 标题取到登录弹窗的 | `document.querySelector('.title')` | `h1.title`（正文页唯一）；`div.title` 是登录框的须排除 | 2026-10-12 |
| 评论列表父子错乱 | 全页 `.list-container` 一把梭 | 有两层同名 `.list-container`：外层=顶层评论，内层在 `.reply-container` 里=子回复；用 `[...qa('.comments-container .list-container')].find(lc => lc.querySelector(':scope > .parent-comment'))` 锚定外层 | 2026-10-12 |
| 主评论选中子回复 | `.comment-item` 直接选 | 子回复 class = `comment-item comment-item-sub`（带 comment-item）；主评论用 `:scope > .comment-item` 排除 | 2026-10-12 |
| 子回复提为 0 | `.comments-container .comment-item-sub` 全局选 | 按父评论定位 `.parent-comment > .reply-container > .list-container > .comment-item-sub`（**平铺**在内层 list，非嵌套） | 2026-10-12 |
| 评论图漏抓 | 只抓正文图 | 评论图在 `.comment-picture img.inner`，src 含 `/comment/`；filter `/\/comment\//`（头像/表情 `img.note-content-emoji` 不含该串，天然排除） | 2026-10-12 |
| 「展开 N 条回复」点不动 | Playwright `.click()` | 游客态登录遮罩 `.reds-mask` 拦截 pointer events，`.click()` 超时；改 JS `el.click()` 强制点击（`.reply-container > .show-more`） | 2026-10-12 |
| 滚动评论不加载 | `window.scrollTo` | 评论在 `.note-scroller`（`overflow-y:scroll`）内，从外层 list 向上找第一个可滚动祖先再 `scrollTop=scrollHeight` | 2026-10-12 |
| 登录态判成游客误报 | 查 cookie `web_session` 是否存在 | ⛔ 游客态也带 `web_session`（设备态）；改查顶栏 `input#search-input` placeholder 是否含「登录」（主）+ `.login-modal` 弹窗（辅） | 2026-10-12 |
| 登录态 6s 就断言"已登录" | 只等一次 DOM | 登录弹窗 ~6-7s 才弹出，须轮询 ~10s 窗口再下结论 | 2026-10-12 |

## 遗留问题

- **游客态评论封顶约 10 条主评论**：滚动 `.note-scroller` 不触发加载更多（实测 `共 873 条` 只渲染 10 条，滚 5 次仍 10 条）。**登录后能否滚动加载全量尚未验证**（本次均按游客态开发）。要抓全量评论须先登录再复测。
- **登录态判据的"已登录"侧未实测**：本机一直游客态，`input#search-input` placeholder 登录后的实际取值未见样本；门禁对"已登录"的判定是**无游客信号的推论**（fail-safe），登录后应复测一次确认不误报 `[ABORT]`。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-10-12 | 初始创建（由 browser_write_lesson 自动生成） |
| 2026-10-12 | 追加 已知坑（来自 https://www.xiaohongshu.com/explore/6acb4c47000000000b0055ab） |
| 2026-10-12 | 补 元信息/网站概况/登录态与限制/反爬/字段提取表/布局变体/URL 规则/图片加载/评论动态/遗留问题（首次实现 extract_xiaohongshu.py 时） |
| 2026-10-12 | 追加 登录态门禁与判据（游客态拒绝执行 + `--allow-unlogged`；主判据=顶栏搜索框 placeholder；cookie 不可用） |
