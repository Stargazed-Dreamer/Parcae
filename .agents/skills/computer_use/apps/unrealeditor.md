---
aliases: ['Unreal Editor', 'UE5', '虚幻编辑器', 'UnrealEditor.exe', 'cs2ue']
---

# UnrealEditor.exe (UnrealEditor.exe)

## 元信息

| 属性 | 值 |
|------|------|
| **process_name** | UnrealEditor.exe |
| **class_name** | （待补） |
| **UIA 友好度** | 不支持（2026-09-22 实测：Slate 自绘，UIA 树恒 1 个节点＝窗口标题本身，且接口自报 `fallback_reason=None` 成功，不可据此认为"屏幕无内容"） |
| **首次接触日期** | 2026-09-22 |
| **相关 task_type** | （待补） |
| **文件创建日期** | 2026-09-22 |
| **最后更新** | 2026-09-22 |

## 软件概况

（待补：这是什么软件、agent 通常在上面做什么任务）

## 软件识别

| 标识 | 值 | 备注 |
|------|------|------|
| process_name | UnrealEditor.exe | 主键 |
| class_name | （待补） | 辅助消歧 |
| 窗口标题模式 | （待补） | 动态部分标注 |
| UWP/WinUI 多层 HWND | 否 | （是则说明 ApplicationFrameHost 外框+子窗口） |

## UIA 友好度与定位策略

| 控件类型 | UIA 支持 | 推荐 locator | 备注 |
|----------|---------|-------------|------|

## 常用快捷键

| 操作 | 快捷键 | 备注 |
|------|--------|------|

## 菜单路径（常见任务）

| 任务 | 路径 | 备注 |
|------|------|------|

## 对话框处理

| 对话框 | 处理方式 | 备注 |
|--------|---------|------|

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|

## UnrealEditor.exe（UE 5.8.2 launcher 版，中文界面）— 2026-09-22 实测

引擎本机位置由注册表真源：`HKLM\SOFTWARE\EpicGames\Unreal Engine\5.8\InstalledDirectory` → `<data_drive>:/<apps_root>\UE_5.8`（不是 Program Files\Epic Games）。

| 现象 | 实测证据 | 正确做法 |
|---|---|---|
| UIA 语义树在 UE 上完全空，且接口自报成功 | `POST /screen/uia/snapshot` depth 8/14、`interesting_only` 两种取值结果全同：**elements 只有 1 个（窗口标题本身）**，而 `fallback_reason=None` | Slate 自绘不透出 UIA。**不要沿用"UIA 优先"策略**，直接把 `节点数<=1` 当作降级条件跳 OCR，否则会误判成"屏幕上没东西" |
| `force_fullscreen_crop=True` 拿到的是别的窗口 | 同 hwnd 同 mode=window，加该参数后 65 个 OCR 框**零个属于 UE**，内容是当时覆盖该区域的窗口（本机为 VMware 窗口）与桌面图标名 | 读 UE 窗口一律走默认 PrintWindow 路径。该参数抓的是"屏幕像素上实际显示的东西"，目标窗口被遮挡/不在前台时必然拿到遮挡者 |
| 担心 DirectX/RHI 自绘导致截图黑屏 | 不成立：窗口截图 1608x908，近黑像素 **0.0%**，全图均值 109，深色菜单栏区域均值仅 27 | 无需 `force_fullscreen_crop`，PrintWindow 对 UE 编辑器有效 |
| OCR 框数少被误判成"截图失败" | 空关卡下 PrintWindow 截图只回 49 框 / 209 字，但中文菜单栏 `文件/编辑/窗口/工具/构建/平台/选择/Actor/帮助` conf 全部 0.9999-1.0，并读到 `大纲`、`细节`、`选择一个对象来查看细节。`、`138个Actor(138个已加载)` | **框数少先怀疑界面本来就没什么文字**，不要归因截图坏。需要小字更准用 `region` 参数做局部 OCR |

### 比屏幕更好的上下文通道：UE 自己的文本

- 工程日志 `<Project>/Saved/Logs/<Project>.log`。**交互式会话确实记录用户动作**：`LogPlayLevel`（PIE 创建/世界拷贝/`World Init took` 时序）、`LogSavePackage`（带资产包路径如 `/Game/test_1`）、`LogEditorClassViewer: Warning: Class ...`（能反推用户打开过"添加组件"类选择器及候选类名，这是 OCR 拿不到的意图信号）。
- 无头跑测命令形状：`UnrealEditor.exe <uproject> -ExecCmds="Automation RunTests <tag>; Quit" -TestExit="Automation Test Queue Empty" -unattended -nopause -nullrhi -NoSplash -stdout -log`。
- 跑测判据优先级：`Result={Fail}` 是否出现 > `Setting GIsCriticalError` 声明行 > `EXIT CODE` 数值（**失败是 -1，不是 0**）> 收尾行存在性。单看 `EXIT CODE: 0` 不等于全绿，须核对 `Test Started` 条数符合预期（防"测试根本没跑到"被当成通过）。
- 单轮跑测日志 1917 行 / 252KB，有效信号仅约 2148 字节（117x），按 `LogModuleManager`/`LogPluginManager`/`LogCsvProfiler` 等前缀整类丢弃即可。
- 恒需忽略的噪声：启动期 13 条 `LogAutomationTest: Error: Condition failed`（红跑绿跑位置条数完全一致）、`LogHttp: Warning: HTTP request timed out ... google.com/generate_204`、`LogSlate: Warning: Unable to rasterize ...AssetIcons\*.svg`。

### 判据纪律（本条教训来源）

曾用 `EXIT CODE: [0-9]*` 扫描，误判"失败时没有收尾行"——实际写的是 `EXIT CODE: -1`，负号没进字符类。**在这台机器上得出"某字符串不存在"的结论之前，先怀疑自己的匹配式。**


## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-22 | 初始创建（由 screen_write_lesson 自动生成） |
| 2026-09-22 | 追加 已知坑（来自 cs2ue - 虚幻编辑器） |
