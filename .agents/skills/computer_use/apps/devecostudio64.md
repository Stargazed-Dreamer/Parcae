---
aliases: ['harmonyos ide', 'devecostudio64.exe', 'deveco studio', 'arkts previewer', 'deveco']
---

# devecostudio64.exe (devecostudio64.exe)

## 元信息

| 属性 | 值 |
|------|------|
| **process_name** | devecostudio64.exe |
| **class_name** | （待补） |
| **UIA 友好度** | （待补：友好/部分/不支持） |
| **首次接触日期** | 2026-09-25 |
| **相关 task_type** | （待补） |
| **文件创建日期** | 2026-09-25 |
| **最后更新** | 2026-09-25 |

## 软件概况

（待补：这是什么软件、agent 通常在上面做什么任务）

## 软件识别

| 标识 | 值 | 备注 |
|------|------|------|
| process_name | devecostudio64.exe | 主键 |
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
| DevEco 是 IntelliJ/Swing(SunAwtFrame)，UIA 树几乎无用 | screen_accessibility_snapshot 拿不到有意义控件 | 用 vision_locate(返回全局坐标) + execute_action 坐标点击驱动 | 2026-09-25 |
| capture_screen 的 window_rect 可能返回**过期偏移**（如 326,30），与 list_windows 真实 rect（如 154,91）不一致 | 按 capture_screen 偏移算的全局坐标点偏，execute_action 报“坐标不在窗口范围内” | 点击前用 list_windows 取权威 bbox；window 模式截图与窗口 1:1，global = 真实origin + 图像像素坐标。vision_locate 用的也是 capture 的偏移，需按真实 origin 重算 | 2026-09-25 |
| execute_action 默认 strategy=auto，点 ArkTS 预览器画布会被解析成 UIA invoke(element_role=Image)，**不派发真实触摸**给 ArkTS 组件 | 点击“成功”但预览器内按钮 onClick 不触发、界面无变化 | 预览器内交互一律 strategy="event" 强制原始鼠标；event 值支持：auto/event(强制原始键鼠)/uia_only | 2026-09-25 |
| ArkTS 预览器首次渲染慢 | 打开预览器后长时间“正在加载预览…/start Daemon” | 等 ~45s 再 capture；首次要编译 ArkTS + 起预览守护进程 | 2026-09-25 |
| 预览器是**渲染级**环境，不预装系统应用 | 跨包显式 Want(bundleName=com.huawei.hmos.settings) 点击后报“可能发生了预览错误”(01/02)，startAbility 抛异常进 catch | 预览器只能证“代码可编译+事件已触发+跨包跳转被预览环境拒”；“真实拉起系统设置/其他 App”须真机或带完整系统镜像的模拟器 | 2026-09-25 |


## 遗留问题

（无）

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-09-25 | 初始创建（由 screen_write_lesson 自动生成） |
| 2026-09-25 | 追加 已知坑（来自 exp3_OpenSettings – Index.ets [entry]） |
