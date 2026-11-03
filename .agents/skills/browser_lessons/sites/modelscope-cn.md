---
aliases: [魔搭, 魔搭社区, ModelScope]
---

# ModelScope 魔搭社区 (modelscope.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | modelscope.cn |
| **首次接触日期** | 2026-07-24 |
| **相关 skill** | modelscope_model_update（扫描前沿模型 + 验证 API 来源 + 写入 keys.json） |
| **登录态要求** | 匿名可浏览模型列表/详情页；调 API 需 ModelScope token（写到 keys.json） |
| **抓取频次** | 周期（按需，新模型发布时） |
| **文件创建日期** | 2026-07-24 |
| **最后更新** | 2026-10-11 |

## 网站概况

阿里达摩院开源模型社区。agent 任务主要是扫描支持 API 调用的前沿模型、验证"魔搭社区"为 API 提供方、收集候选清单供 keys.json 更新。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | **禁止** | 用户要求 + 本环境无头报错 |
| 登录态来源 | 匿名浏览即可 | 调 API 才需 token |
| 反风控 | 不主动调站内 API，纯 DOM 提取 | 模拟人类点击+等待 |

## 反爬/风控

- **触发条件**：未观察到主动反爬
- **规避方法**：用 DOM 提取 + 合理等待（3.5 秒/页），不调私有 API
- **风险阈值**：单次扫描 4 方向 × 2 页 × 10+ 模型详情页未触发限制

## DOM 结构与提取规则

### 入口 URL

```
https://modelscope.cn/models?filter=inference_type&page=1&tabKey=task
```

- `filter=inference_type`：**预选**只展示支持 API 调用的模型（必带）
- `tabKey=task`：按任务分类展示
- `page=N`：翻页参数
- `tasks={task_name}`：按任务过滤（点击左侧任务标签后追加，也可直接构造）

### 页面布局

- **列表页**：左侧过滤面板（`div.acss-d9h0gd`，w=256）+ 中间模型卡片列表 + 底部分页
- **详情页**：左侧模型说明正文（left=0, w=872）+ 右侧 API 面板（left=896, w=384）

### 字段提取表

| 字段 | 选择器/规则 | 注意事项 |
|------|--------|---------|
| **模型 ID** | 正则 `^/models/([^/]+/[^/\?]+)` 从 `a[href*="/models/"]` 提取 | **不从卡片标题取**——卡片标题可能含装饰文本；用 Set 去重（同一模型多页重复） |
| **卡片简介** | 卡片 `innerText` 切片前 400 字 | 会混入下载量/点赞数，需清洗 |
| **API 提供方** | 右侧面板 `div.acss-17aobl4` 的 innerText | **必须点开详情页**，列表卡片不显示提供方 |
| **API 面板标题** | 文本含 `推理 API` + `Inference` | 选择器可能改版，用文本特征回退定位（见下方） |
| **魔搭社区标签** | 面板文本含 `魔搭社区` | 仅显示 OpenAI/Anthropic 的不可用 |

### 左侧任务过滤项与项目方向对应

| 项目方向 | ModelScope 任务名 | 位置 |
|---------|------------------|------|
| LLM | "文本生成" | 热门任务区 |
| VL | "视觉多模态理解" | 热门任务区 |
| aigc_image | "文本生成图片" | 热门任务区 |
| aigc_video | "文本生成视频" | 热门任务区 |
| TTS | "语音合成" | 热门任务区 |
| ASR | "语音识别" | **不在热门任务区**，需展开"语音"分类 |

### 选择器优先级与回退

```javascript
// 右侧 API 面板定位（选择器可能改版，必须加文本回退）
let panel = document.querySelector('div.acss-17aobl4');
if (!panel) {
    const all = document.querySelectorAll('div, aside, section');
    for (const el of all) {
        const t = (el.innerText || '');
        if (t.includes('推理 API') && t.includes('Inference') 
            && el.getBoundingClientRect().left > 700) {
            panel = el;
            break;
        }
    }
}
```

### API 来源判断规则

```javascript
const text = panel?.innerText || '';
const hasTitle = text.includes('推理 API') && text.includes('Inference');
const hasModelscope = text.includes('魔搭社区');
// 可用 = hasTitle && hasModelscope
// 仅外部源 = hasTitle && !hasModelscope && (text.includes('OpenAI') || text.includes('Anthropic'))
// 无面板 = !panel
```

## URL 规则与重定向

- **直接构造 tasks URL 比点击过滤项更可靠**：`https://modelscope.cn/models?filter=inference_type&page={N}&tabKey=task&tasks={task_name}` 直接 goto，避免左侧面板点击失败
- **详情页 URL**：`https://modelscope.cn/models/{model_id}`（model_id = `{author}/{name}`）
- **URL 编码**：tasks 参数为中文任务名，Playwright goto 会自动编码

## 图片/资源加载

| 项 | 规则 | 备注 |
|----|------|------|
| 懒加载 | 未观察到 | 卡片图片即时加载 |
| 防盗链 | 未观察到 | 模型示例图可直接下载 |

## 评论/动态内容

（无，ModelScope 模型页无评论系统）

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| **`networkidle` 等待超时** | 用 `wait_until="networkidle"` + 短 timeout | 用 `domcontentloaded` + `wait_for_timeout(3500)` 兜底 | 2026-07-24 |
| **右侧 API 面板未及时渲染** | goto 后立即抓面板文本 | `wait_for_timeout(3500~4000)` 后再抓取 | 2026-07-24 |
| **仅看模型卡片判断 API 可用性** | 卡片不显示 API 提供方就跳过 | 必须点开详情页查右侧面板 | 2026-07-24 |
| **从卡片标题取模型 ID** | 直接取卡片标题文本 | 用正则从 `a[href*="/models/"]` 的 href 提取 `{author}/{name}` | 2026-07-24 |
| **同一模型在多页重复出现** | 直接 append 导致重复 | 用 Set 按模型 ID 去重 | 2026-07-24 |
| **卡片 innerText 混入导航/下载数** | 整个卡片 innerText 当简介 | 用正则限定 `/models/{author}/{name}` 模式，简介取前 400 字并清洗 | 2026-07-24 |
| **TTS/ASR 在 `filter=inference_type` 下返回 0 结果** | 当 bug 调试 | **正常**——ModelScope 当前无支持 API 调用的 TTS/ASR 模型，语音模型均需本地部署 | 2026-07-24 |
| **ASR 不在热门任务区** | 在热门任务区找"语音识别" | 展开"语音"分类才看到"语音识别"子任务 | 2026-07-24 |
| **点击左侧任务标签失败** | 反复重试 click | 直接构造 `&tasks={task_name}` URL goto | 2026-07-24 |
| **右侧面板选择器 `acss-17aobl4` 失效** | 报错退出 | 用文本特征（"推理 API" + "Inference" + left>700）回退定位 | 2026-07-24 |
| **仅显示 OpenAI/Anthropic 的模型被误采** | 看到 API 面板就认为可用 | 必须验证面板含"魔搭社区"标签 | 2026-07-24 |
| **面板"魔搭社区"标签漏判（反向误杀）** | 面板判 external_only 就放弃该模型 | 面板仅初筛，最终以真实 token API 实测（stream=true）为准——Qwen/Qwen3.5-35B-A3B 面板无魔搭标签但 API 实测 200 可用 | 2026-09-13 |
| **把面板上的 "OpenAI / Anthropic" 当成 API 提供方** | 看到 OpenAI/Anthropic 就判"外部源可用"，或据此判"魔搭已托管" | 那两个是**协议兼容 tab**（OpenAI 兼容 / Anthropic 兼容），不是提供方名单；提供方是否绑定只能靠真实 API 实测。2026-09-19 实测 Qwen3-VL-235B 面板只剩这两个 tab，API 已 400 | 2026-09-19 |
| **模型被下架时误判"调用机制变了"** | 反复改 base_url/鉴权/请求体 | `400 {"error":{"message":"Model id : X , has no provider supported"}}` = 该 Model-Id 在魔搭**没有任何推理提供方绑定**（下架），端点/协议/token 都没变。官方文档明示"旧模型可能下线不再支持，请配置当前支持的模型 ID" | 2026-09-19 |
| **想用 `:<Provider>` 后缀救回下架模型** | 给下架模型拼 `:DashScope` 重试 | 后缀只对"有外部提供方绑定"的模型有效。错误码可区分：`Invalid provider: X` = 提供方名非法；仍报 `has no provider supported` = 模型本身无绑定，后缀无效（实测 Qwen3-VL 8B/235B 两种后缀都失败） | 2026-09-19 |
| **已上架模型可能转付费** | 404 当成下线反复调试 | 404 "This model is unavailable for free. The paid version is available now" = 模型转付费（Tencent-Hunyuan/Hy3），免费 token 无法调用 | 2026-09-13 |
| **inclusionAI Ring 系 401 ling_auth_not_exist** | 以为是 token 失效去换 token | Ring 系走 inclusionAI ling 独立授权层，通用 ModelScope token 不通（同 token 调 Ling-3.0-flash 正常） | 2026-09-13 |
| **`?search=` 参数被静默忽略** | 用 `https://modelscope.cn/models?search=piano` 构造搜索 URL，期望得到过滤结果 | 实测三个不同关键词（piano / transcription / 钢琴）返回**完全相同的 30 个默认模型 ID**（Qwen/MiniMax 等）——参数被吞，拿到的是默认列表。要搜必须用页面搜索框交互，或先抓 XHR 找真参数名，**不要拿参数名硬试**（会可见地反复刷新用户屏幕上的页面） | 2026-09-21 |
| **想拿 token 做程序化全局搜索** | 用 `keys.json` 里的 ModelScope token 打 `POST /api/v1/models` 当关键词搜索端点 | 该端点是**按 owner 列模型**，不传 `Path` 时报 `400 Code=10010202003`。`Authorization: Bearer <token>` 鉴权可过（对比未鉴权的 `401 user not logged in`），`Cookie: m_session=<token>` 仍 401。**没有匿名的全局关键词搜索 API** | 2026-09-21 |
| **`/search?search=` 页是 SPA 壳** | 对该 URL 做 HTTP GET 后解析 HTML 找模型 ID | 只返回 4311 字节空壳、0 个 ID。要走 `/models` 列表页（SSR 出卡片）+ `a[href*="/models/"]` DOM 提取 | 2026-09-21 |
| **判"魔搭可不可达"只看 DNS** | `gethostbyname` 解出 IP 就认为能下模型权重 | 数据面真路径 `https://www.modelscope.cn/models/<ns>/<name>/resolve/master/<file>` 实测 200 / 10036 B 可达。**这条比 hf-mirror 可靠**：hf-mirror 元数据面（`HfApi.list_repo_files`）通，但 `/resolve/main/` 数据面 ConnectTimeout | 2026-09-21 |
| **图片异步任务不带 async 头** | 裸 `POST /v1/images/generations` 拿到 200+task_id 就以为提交成功 | 部分模型（GLM-Image）400 报错原文给出文档：必须带 **`X-ModelScope-Async-Mode: true` 头**。Qwen/FLUX 系不带头也能拿 task_id，但任务会坏死——最终以任务轮询结果为准 | 2026-10-11 |
| **"提交 200+task_id 但任务秒 FAILED"当可用** | submit 200 就判"魔搭托管可用"写入 keys.json | 轮询 `GET /v1/tasks/{task_id}` 见 `task_status: FAILED` + `{"code":500,"message":"task not found"}` = 免费层接受提交但**无推理绑定**，实际不可用（2026-10-11 实测 Qwen-Image-2512/2.1、Edit-2511、FLUX.2-dev/klein-9B 五个模型全如此，带不带 async 头/size 都一样）。可用性判据必须是任务 SUCCEEDED | 2026-10-11 |
| **视频生成端点瞎猜** | 试 `/video/generations` 或统一 `POST /v1/tasks` | 视频真端点是 `POST /v1/videos/generations`（其余 404 page not found）。该端点报 `500 {"code":400,"message":"Invalid model provider"}` = 视频版"无免费 provider 绑定"（等价 chat 的 `has no provider supported`）；`:DashScope` 后缀救不回（报 has no provider supported），async 头无效 | 2026-10-11 |
| **把 `submit failed with code: 40212` 当网络/鉴权问题** | 反复重试或换 token | 40212 = 图片任务提交被拒（GLM-Image、Qwen-Image-2.1-PE 系实测），具体语义待考，但结论就是免费层不可用，别浪费重试 | 2026-10-11 |
| **把 `Invalid model id` 当模型下架照旧重试** | 认为和 `has no provider supported` 同义 | 400 `Invalid model id: X` = 该 id 本身已不存在（改名/删除，2026-10-11 实测 MiMo-V2.6-Pro），应回扫描清单核对新 id 而不是重试 | 2026-10-11 |

## 遗留问题

- **TTS/ASR 方向无 API 模型**：ModelScope 在 `filter=inference_type` + 语音合成/语音识别过滤下"共找到 0 个结果"。未来若项目加入 TTS/ASR 能力，需考虑其他供应商（阿里云语音 NLS、讯飞开放平台等）
- **aigc_image/aigc_video 模型 API 调用方式不同**：图片生成是异步任务（`POST /v1/images/generations` + `GET /v1/tasks/{task_id}` 轮询），与 LLM 的 OpenAI 兼容接口不同。写入 keys.json 时需确认项目的 aigc 调用层支持异步任务模式
- **DOM 选择器为 2026-07 探索结果**：`acss-17aobl4` / `acss-d9h0gd` 等为构建产物类名，改版后会失效。已加文本特征回退，但回退本身也需未来验证

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-07-24 | 初始创建，汇总自 `temp/modelscope_explore/` 探索结果和 `modelscope_model_update` skill 首次扫描 |
| 2026-09-13 | 二次扫描（LLM+VL）新增 3 坑：面板魔搭标签漏判须 API 实测兜底、模型转付费 404 特征、Ring 系 ling 独立授权 401 |
| 2026-09-19 | 排查 VL 连续 400 新增 3 坑：面板 OpenAI/Anthropic 是协议 tab 不是提供方、`has no provider supported` = 模型下架（非机制变更）、`:Provider` 后缀救不回下架模型；同步核查官方文档（端点/鉴权未变，新增 Responses API 与 Anthropic 兼容） |
| 2026-09-21 | 查钢琴转录权重时新增 4 坑：`?search=` 被静默忽略、`POST /api/v1/models` 是按 owner 列而非全局搜索（Bearer 可鉴权但需 `Path`）、`/search` 页是 SPA 壳、判可达性要看数据面 `/resolve/master/`（实测通，优于 hf-mirror 数据面超时）。⚠️ 本次 `browser_write_lesson` 写不进来：该端点 section 白名单只收 `已知坑`，而本文件实际标题是 `已知坑（与正常行为区分）`，两条报错互相打转，故改为手工编辑 |
| 2026-10-11 | 月更全 4 方向零可用，新增 5 坑：图片异步需 `X-ModelScope-Async-Mode: true` 头、"提交 200 但任务秒 FAILED task not found"=免费层无推理绑定（不可用新判据）、视频端点 `/v1/videos/generations` + `Invalid model provider` 语义、`submit failed 40212`=提交被拒、`Invalid model id`=id 已不存在（区别于下架） |
