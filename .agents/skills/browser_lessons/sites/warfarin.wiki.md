---
aliases: [warfarin.wiki, 华法琳Studio, 华法琳 wiki, studio.warfarin.wiki, 终末地3D查看器]
---
# warfarin.wiki / 华法琳Studio (warfarin.wiki)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | warfarin.wiki |
| **首次接触日期** | 2026-10-27 |
| **相关 skill** | browser_lessons |
| **登录态要求** | 匿名可访问（无登录） |
| **抓取频次** | 一次性（渲染实现分析） |
| **文件创建日期** | 2026-10-27 |
| **最后更新** | 2026-10-27 |

## 网站概况

《明日方舟：终末地》同人资料站。`warfarin.wiki` 是 wiki，`studio.warfarin.wiki` 是 3D 模型查看器
（干员/NPC/武器/敌人/设备 + 动作、音效、背景、相机动画、光照/阴影/视觉特效开关）。
技术栈：**SvelteKit + three.js r170**，自定义 Unity HSRP 风格 NPR 材质。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `POST /browser/sessions`（后端 session） | 项目统一 |
| 首屏 | 弹「欢迎…管理员！」模态框，需点「确定」或「一天内不再显示」 | 不点也不影响 `evaluate` 读 DOM |
| 反爬 | 未见（Cloudflare Insights 埋点 + 扩展注入） | 不需要伪装 |

## 反爬/风控

（无）。首屏 modal 是产品提示，不是人机校验。

## DOM/前端特征

- **SPA（SvelteKit）**，所有代码在 `_app/immutable/`，文件名带内容 hash（**每次发版都会变**，
  别把 `BKD6jSn4.js` 这类名字写死进脚本，用 `performance.getEntriesByType('resource')` 现取）。
- 只有 **1 个 canvas**，`WebGL2`，`antialias/alpha/stencil` 全开，`preserveDrawingBuffer:false`；
  `renderer.setClearColor(0,0)` ⇒ **canvas 透明，背景渐变是 CSS**（截图取色不等于渲染结果）。
- 页面底部是模型选择轮播（干员/NPC/武器/敌人/设备 五个 tab），点击才加载 GLB。
- 站方提示「会消耗大量模型数据流量」——**只做代码分析时不要点模型**，避免白下几十 MB。

## 渲染实现（2026-10-27 已完整拆解）

- 引擎：内联 three r170，**无 EffectComposer / 无屏幕后处理 / 不用 three 灯光**。
- 材质：`new ShaderMaterial({name:"<名> (toon)"})`，顶点/片元各 860 B / 17 KB；
  由 Unity 材质属性 `_UseDiffRampMap`、`_UseSDFLightmap`、`_UseMatcap`、`_LineMap`… 驱动 `#define`。
- **GLSL 标识符被构建期统一改名成 `Q###`**（JS 侧 uniform key 同名，两边一致），
  逻辑完整但名字无语义；要还原语义得从 JS 侧 `l._MatcapTex` 这类属性名反推。
- 光照：单主光跟随相机（yaw +10°、minPitch 32°、强度 2×0.9），暗部 tint 偏蓝、皮肤暗部偏暖；
  片元内有 ≤16 盏补光循环；自阴影是**运行时渲染的阴影图 + 屏幕空间 4-tap 采样**，不是 three shadowMap。
- 描边：独立 pass，`BackSide` + **clip 空间 XY 外扩**（Z 单独处理，保证等宽）+ 像素级钳制；头发用 stencil(16) 去重。
- 详细报告与提取出的着色器：`temp/warfarin_probe/`（`REPORT.md` + 4 个 `.glsl` + uniform 对照表）。

## 踩坑记录

- 2026-10-27：用 `/browser/supervisor/ensure` 起浏览器 ⇒ Chrome 起来又退（supervisor 报
  「9222 被非本 supervisor 进程占用」）。**起浏览器仍按项目规范走 `terminals/spawn` + `start_debug_browser.py`**，
  supervisor 那条路不可靠。
- `browser/evaluate` 里带正则的表达式经 curl `-d` 传容易撞 JSON 转义（`Invalid JSON`）；
  建议写成 `.js` 文件 + Python 读文件发 POST（参考 `temp/warfarin_probe/ev.py`）。
