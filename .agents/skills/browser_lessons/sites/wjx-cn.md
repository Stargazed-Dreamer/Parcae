---
aliases: ['wenjuanxing', 'wjx', 'wjx.cn', '问卷星']
---

# wjx.cn (wjx.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | wjx.cn |
| **首次接触日期** | 2026-09-19 |
| **相关 skill** | （待补） |
| **登录态要求** | 需已登录。chrome_debug 的登录态**会过期**（2026-09-28 实测：09-19 记的"已带登录态"到 09-28 已失效，`login.aspx` 停在登录页）。登录/扫码由用户本人完成，agent 不代过；判登录态访问 `login.aspx` 看是否跳「我的问卷」 |
| **抓取频次** | （待补） |
| **文件创建日期** | 2026-09-19 |
| **最后更新** | 2026-09-27 |

## 网站概况

（待补）

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | 禁止 | 用户要求 + 环境不支持 |
| 登录态来源 | 调试浏览器中扫码登录 | 独立 `user-data-dir` |

## 反爬/风控

（无）

## DOM 结构与提取规则

（待补）

## URL 规则与重定向

（无）

## 图片/资源加载

（无）

## 评论/动态内容

（无）

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|
| `browser_snapshot` 在 wjx.cn 首页/编辑器页返回 >130KB 被截断 | 节点多且响应里的 `dom_hash` 字段无上限，`root_selector`/`max_nodes` 都压不住它 | 编辑器类页面禁用 browser_snapshot；改用 Playwright `connect_over_cdp('http://127.0.0.1:9222')` 直连同一实例读 DOM，截图只用 `browser_screenshot(return_base64=false)` | 2026-09-19 |
| 首页导航显示「登录/免费注册」≠ 未登录 | chrome_debug 调试浏览器已带问卷星账号登录态，但 wjx.cn 首页导航按未登录渲染 | 判断登录态要访问 `https://www.wjx.cn/login.aspx`，已登录会直接跳进「我的问卷」 | 2026-09-19 |
| 文本导入语法 | 首行=问卷标题；`N.题干 [填空题]` / `[单选题]`；单/多选选项逐行写 `A.xxx`；`[量表题]`/`[矩阵题]`/`[排序题]` 同样可用；导入后默认**全部必答** | 入口：创建问卷 → 右侧「文本导入」；改题请改文本重新导入，别在编辑器里逐题点 | 2026-09-19 |
| 点「完成」生成问卷会弹腾讯「点击开始智能验证」 | 人机验证，可能升级为滑块/选图 | **必须人工完成，agent 不得代过**；先告知用户再重试提交 | 2026-09-19 |
| 「必答」勾选框 JS 点击无效 | `input[id^=req_]` 被包在 `span.wjx__templet__beautifyInput` 里，`span.click()` 不触发 | 取该 span 内的 `label` 用 Playwright 真实 `handle.click()`；`req_<题号>_<随机>` 的中间数字就是题号，可据此校验点没点错题 | 2026-09-19 |
| 填空题类型校验的选项叫「邮件」不叫「邮箱」 | `select.verify-check-type`（onchange=`cur.setVerify`）选项为 不验证/整数/小数/年月/日期/时间/手机/固话/**邮件**/密码/网址/身份证号/学号/QQ… | 找选项要按 `innerText==='邮件'` 匹配；设完 dispatch `change` 事件才会生效 | 2026-09-19 |
| 属性验证/必答等设置行只对「当前激活题」渲染 | 未激活的题没有这些控件；页面上同时只有一份 | 改某题设置前，先 JS `click()` 它的题干文本激活它 | 2026-09-19 |
| 说明语写进了问卷标题 | `.surveyhead` 里 `querySelector('textarea')` 先命中 `#paper_attr_title`（标题），wangEditor 的说明区是 `.surveydescription .w-e-text`（contenteditable），`#paper_attr_desc` 只是隐藏镜像 | 填标题用 `#paper_attr_title`；填说明用 `.surveydescription .w-e-text` 并 dispatch `input`，之后回读 `#paper_attr_desc` 确认已同步 | 2026-09-19 |
| 保存按钮文本「完成编辑」有 5 个同名节点 | `browser_action` 按 text 定位报 MULTIPLE_MATCHES | 直接用 `css="#hrefFiQ"`；保存后跳 `designstart.aspx?activity=<id>`，页面显示「此问卷处于草稿状态」+「发布此问卷」 | 2026-09-19 |
| v7编辑器批量取消必答 | 必答开关 input[id^="req_N_"] 只对『已激活』的题渲染，且 req 属性选择器里 num 拼接必须写在引号外：'[id^="req_'+num+'_"']'（写进字符串里报 not a valid selector） | 逐题：JS .click() 该 .div_question[idx] 的 .see_div_ttitle 激活→等~700ms→evaluate_handle 取 input[id^="req_N_"] 的 closest('span') 内 <label> 句柄→Playwright 真 .click()（JS click 不生效）；after checked false 即成功 | 2026-09-27 |
| v7填空验证格式 | 选题级 select.verify-check-type（在激活后的 .div_question 块内），options value 是中文本身如 手机='手机'、Email 的文案叫『邮件』 | 激活该题→blk.querySelector('select.verify-check-type')→设 value 后 dispatchEvent(new Event('change',{bubbles:true}))；切走再切回该题读 value 确认持久（实测手机验证 reload 后仍在） | 2026-09-27 |
| 卷首语编辑器假象 | 可见可编辑体是 #pater_desc > div.w-e-text[contenteditable=true]；而 .surveydescription.w-e-text 命中的是隐藏副本，Playwright .click() 它因 actionability 等待会静默卡死 30s（表现为 exec_python 长时间无 stdout） | 用 locator('#pater_desc div.w-e-text:visible') 真 click 聚焦→keyboard.press('Control+A')+Delete 清空→keyboard.insert_text(文本)（注意是 insert_text 不是 insertText）→Enter 换行→blur 后读 #paper_attr_desc.value 确认同步。**⚠ 前提是编辑区已展开**，新建/未点开的页面里 #pater_desc 是 display:none，见下一行 | 2026-09-27 |
| 新页面的卷首语区默认是收起的，上面那条 locator 直接超时 | 在刚生成的问卷编辑器里等 `#pater_desc div.w-e-text:visible`，30s 超时；`#pater_desc` computed display=none | 先真 click `#pater_desc_preview`（占位文案"添加问卷说明"，display:block）把编辑区展开，`#pater_desc` 才会变成 `w-e-text-container` 并挂出 `.w-e-text`，再走上一条的清空+insert_text | 2026-09-28 |
| 文本导入不支持文件/图片上传题 | 指望 `N.题干 [文件题]` / `[文件上传题]` / `[上传题]` 能导入出来（官方帮助 helpid=252 只列到 单选/多选/填空/简答/判断） | 上传题只能在编辑器「题型」面板点 `span:has-text("文件上传")` 追加；免费版可用，渲染出"选择文件( 不超过 10 M )"，重载后 `input[type=file]` 仍是懒加载（判类型用 `blk.innerText` 含"选择文件"，别用 querySelector('input[type=file]')） | 2026-09-28 |
| 面板新加的题一律落在问卷**末尾**，且没有上移按钮 | 先激活第 15 题再从面板插入，以为会插在当前题后面（实测 blocks 33→34，14/15/16/17 顺序不变） | 题栏图标只有 复制/删除/`question_dragbtn`（title="长按移动题目"），没有上移下移。**长按拖拽是可用的**：每次先 `scrollIntoView({block:'center'})` 再取 `.question_dragbtn` 中心，`mouse.down()` 后**等 ~900ms** 才进入拖拽态，分步 move 到上一题 `getBoundingClientRect().top + 4` 处 `mouse.up()`，一个 hop 上移 1 位；从末尾挪到第 22 位就是循环 11 次（实测 33→22 全成）。❌ 之前记的"跨屏拖不动"是因为直接拿目标块坐标没先滚动，算出 -3500 的负 y | 2026-09-28 |
| 题目级删除 vs 选项级删除同名 | `blk.querySelector('.li_del')` 命中的是左侧面板 `.stuff > LI.li_del`（零尺寸），点了没反应也没弹窗，误判"删除不可用" | 题目级删除要用 **`blk.querySelector('.right_control .li_del')`**（右侧悬浮工具栏 x≈1013）；点完直接生效、**不弹确认框**，靠 `.div_question` 数量 -1 校验 | 2026-09-28 |
| 矩阵题有两个 table，维度读错会以为行翻倍 | 用 `blk.querySelector('table')` 或数 `tr` 判行数 → 编辑器镜像表把"行标题"和"选项格"拆成两行，5 行被读成 10 行，误判「批量设置是追加」把结构说坏 | 权威结构取 **`blk.querySelectorAll('table')` 的最后一个**：首行是 `行标题\选项 + 各列名`，其后每行一个行标题。实测 5 行×4 列在镜像表里显示 10 行 | 2026-09-28 |
| 矩阵题行/列批量设置入口不同，语义都是**替换** | 以为行列都在同一个 textarea 弹窗里；或以为「批量设置」是追加 | 行：`a.option_link`（onclick 含 `enlargeRowTitle`）→ 主文档 `#txtEnlarge` textarea，预填当前行，一行一个，点弹窗内「确定」即替换。列：onclick 含 `likertsetbtnclick` → **iframe 弹窗** `wjxdesignnew/module/oftenoptions.aspx`，主文档查不到任何 input，要 `pg.frames` 里找该 frame，写 `#prepop`（列名，一行一个）+ `#prevalue`（对应值 1..N），再在 frame 内点「确定」。列数变少就靠这里删（列头 `.rowset i` 点了没反应） | 2026-09-28 |
| 题号有两种模式，默认自带字面号 | 把 `1.姓名` 这类序号留在题干里，之后移动题目就得手工改号，还会撞出两个"15." | 顶部「隐藏系统题号」勾选框 id=`#chkUseSelfTopic`（勾着=用自带字面号）；**取消勾选 + 点「删除自带题号」**（会弹确认，点 `.layui-layer-btn0`）即切到系统自动编号，实测底层题干变回纯 `姓名`、显示号由系统渲染且随移动自动重排。之后挪题目不用再改号 | 2026-09-28 |
| 题干序号是**字面文本**，移动题目不会自动重排 | 以为问卷星自动编号，把新题拖到位后不改号（导入文本里 `1.姓名` 的"1."是标题的一部分，预览只显示一层序号，说明系统题号是关着的） | 改位置后要手工改题号；或整卷删掉字面序号改用系统自动编号。表头有「隐藏系统题号」「删除自带题号」两个开关可验证 | 2026-09-28 |
| `.see_div_ttitle` 的 innerText 不能当题干真值 | 看到 `8.你实际到场了哪几讲 [多选题]` 就以为导入把类型标签漏进了题干，急着去改题 | `[多选题]` 是编辑器显示的类型角标、激活后还会追加"引用 超链接 图片 音视频"工具栏文字。真实题干读 `.div_field_editor [contenteditable=true]`，实测是干净的 | 2026-09-28 |
| 生成过一次后 `#divPreview` 不再随文本重渲染 | 用「往 textarea 填文本 → 读 #divPreview 有没有 file input」来探文本导入语法，四次探针读到的都是上一份的旧预览（`len=30`，只剩标题+"完成"） | 探针失效≠语法不支持。要验语法只能真生成一份（会在账号里留下问卷）或读官方帮助页；别拿这个预览下否定结论 | 2026-09-28 |
| 共用调试浏览器时，按 URL 子串取 `ctx.pages[0]` 会抓到别的会话的页 | `next(x for x in ctx.pages if 'designqbytxt' in x.url)` 拿到另一会话正在用的标签页：在它里面 fill 文本、点隐藏节点没生效，却弹了**对方的**"生成成功"，把对方的 `curid=386649949` 当成自己新建的问卷（回读内容全是对方题面才发现） | 自己 `ctx.new_page()` 全程持有句柄；必须在多页里认领时，用**内容特征**判（如 textarea `input_value().startswith('我的标题')`），别用 URL 子串、别用页序。落盘判据看「我的问卷」列表里 ID 是否新增 + 标题是否是自己那份 | 2026-09-28 |
| `saveMode(2,this)` 用 JS 直接调不生效 | 在别的会话的页面上 `a[onclick^="saveMode"]` 取到节点后 evaluate 调用（该节点 computed display:none），无跳转无报错，误以为成功 | 真按钮是预览区的 `#divPreview .importRf-set#paperbutton a.wjxui-btn`（文本框有内容且页面正常时才 display:inline-block），用 Playwright 真 `.click()`；成功后弹「生成成功！」→ 点 `.layui-layer-btn0` 才跳 `designnew.aspx?...&curid=<新ID>` | 2026-09-28 |
| 草稿态没有填答链接 | 去 `sendqstart.aspx?activity=ID` 抓 `/vm/xxx.aspx` 想发给同学 | 未发布时该页只有"未发布，请发布后再发送问卷！"，`/vm/` 链接抓不到。发布是对外动作，交给人做（还可能弹腾讯智能验证） | 2026-09-28 |
| 保存/发布路径 | 顶部『完成编辑』#hrefFiQ onclick=save_paper('pub',true)，点完跳 designstart.aspx?activity=ID&fromedit=1，页面显示『此问卷处于草稿状态』——不会自动公开 | 保存后到 designstart 页核对标题+ID+『草稿状态』文案即确认落盘；本次未触发腾讯智能验证（此前某些保存会弹，须人工过，agent 不代过） | 2026-09-27 |
| 我的问卷列表URL | /newwjx/mysojump/list.aspx 及多个猜测 manage/designlist/myquestionnaire.aspx 全 302→Error/404.aspx | 别硬猜直链；从编辑页/账号悬浮菜单取真实 href，或让用户在 UI 进『我的问卷』再操作；清理废弃草稿属破坏性动作先给清单再删 | 2026-09-27 |



## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-19 | 初始创建（由 browser_write_lesson 自动生成） |
| 2026-09-19 | 追加 已知坑（来自 https://www.wjx.cn/wjxdesignnew/designnew.aspx） |
| 2026-09-27 | 追加 已知坑（来自 https://www.wjx.cn/wjxdesignnew/designnew.aspx?version=7&curid=386608808） |
| 2026-09-28 | 追加 10 条 已知坑（新建结课作品问卷 curid=386649903：文本导入→必答/验证→卷首语→上传题→草稿保存 全链路）；给 09-27 的卷首语条目补"须先点 #pater_desc_preview 展开"前提 |
| 2026-09-28 | 自我纠错 4 条：长按拖拽**可用**（先前误记"跨屏拖不动"，实为没先 `scrollIntoView` 导致目标 y 算成负数）；矩阵「批量设置」是**替换**不是追加（误判源于读了编辑器镜像 table）；题目级删除选择器是 `.right_control .li_del`；题号可切系统自动编号（`#chkUseSelfTopic` + 「删除自带题号」），此后移动题目不用再改号 |
