# 发布历史

本文件由发布流程自动生成,记录每个公开版本的演进脉络:
版本间包含的私有仓库 commit 数量、开发时间跨度与主要更改摘要
(摘自 CHANGELOG,仅取条目标题)。

<!-- 由 tools/release/engine/history.py 维护,请勿手工编辑 -->

## 0.53.0
- 首次发布,无区间统计
- 主要更改:
  - chat_digest 的 v4 同步换成 mtime 增量快路径
  - `ask_user` 从"清单里有但从没接线"变成会话时间线里的一张交互卡片
  - 压缩判据改成「上一次调用的真实 prompt_tokens + 之后新增消息的字符增量」，上下文上限来自 keys.json 的 model 级 `context_window`，没填就报"上限未知"且不判压缩
  - 死链闸门 C6 上闸：在用文档面引用的仓库内路径必须解析得到，存量走机器扫出的基线名单
  - 祖先链那一格是本轮实测纠的自己的错
  - 名单不是橡皮图章
  - 摘改矩阵抓到一个真缺口
  - ⚠️ **洗存量这一档没做完，而且比想象的贵**：49 条按「同名文件能否查得」分档 = 18 条有唯一同名目标 / 9 条多目标 / 22 条磁盘上根本没有。
  - 作废一条混口径记录：账本原写「148 条中 75 条不可解析」——分子取自 commit `674893a` 的 docs 口径（当时死链 32），分母 75
  - VL Ticket 04：两级状态视图 + `POST /vision/reset`，聚合判据补上"整条 key 的模型当日全判死"
  - `POST /headless/sessions`：直连创建一个 headless 会话，不必再绕 LoopManager
  - Ticket 02(a) 正式裁决：5xx 连败计数与冷却保持 key 级连坐，代价改成看得见的
  - 异环采集器把"要人复核的稀有度"从一句话升级成一份可查证据（裁片 + 索引落盘）
  - VL 降级链的「同 token 串行」守卫单测 D8（Ticket 02 的"不得放松"项）
  - `(key_id, model)` 级「今日永久失效」标记：死模型不再每轮重新烧一遍
  - migrate_changelog 归档完自动复跑一次发布口径扫描，HIGH 复活当场拦下
  - gate 账本三方一致性闸门 `tests/release_ci/test_gate_ledger_consistency.py`
  - 内核对象不回收：新增两支只读探针，并按 `report/根因测试路线_20261005.md` 完成阶段 0—2 交付
  - gh_mirror「添加跟踪」的默认流程改成 Actions 服务器端导入 + 前置检查，本地零下载
  - gh_mirror 的 6 个新导入仓补齐 `release-backup.yml`，44 个 enabled 仓实现全覆盖
  - gh_mirror 新增跟踪仓 `NapNeko/NapCatQQ`，并把 release-backup 铺满全部 38 仓
  - release 备份新增范围限制 `--since-days` / `--limit`，可按仓配置、手动可覆盖
  - release 稀疏清理的完整考量归档为可重启的计划
  - 远程 VL 的"死模型"处置：400/404/422 判永久失败、同 key 换下一个模型、响应体进日志
  - gh_mirror 远端 workflow 漂移体检（只读）
  - 记忆召回判卷标注器，把"该读到哪几条"交给人判
  - chat_digest 常驻「引用↔编号」校验器 `workspace/chat_digest/verify_quotes.py`
  - 构建期 gate `no-key-startup` 改名 `compile-check`，并去掉"只取前 200 个 `.py`"的静默上限
  - video_digest 的 STT 后端正式从 whisper 切到 r2t2_stt，新增默认驱动桥 `r2t2_bridge.py`
  - 整批一次装载
  - SRT 地板按片长取档，两档都是本机实测
  - ⚠️ **机制没有定案，别再写成因果**：昨天把"崩"归因到"越物理上限 → `cudaFree` 归零 → 每步在零空闲设备上申请 → 慢 10–40 倍"。
  - SRT 会丢内容，缺口不等于静音
  - 片长默认取上游常量 `ALIGNER_INTERNAL_CHUNK_SEC`（180s），一片正好对应 aligner 内部一块；片尾短于 5s 并进前一片——
  - 时间戳这一档禁止任何形式的自动决定
  - 幂等 no-op 不再被显存守卫误拒
  - SKILL 里两条今天已失效的论断删除：「`STT_PY` 仍指向 whisper」、「每片一个新进程 + `load()` 后 dummy 预分配可解挂死」。
  - 验证全部实跑：`uv run pyright` 显式对 6 个文件 **0 错**（`workspace/` 不在仓库级 include 内）；30s 样本 T
  - gh_mirror「这个仓是否已下发」改成运行时判定，手抄的 `PILOT_DONE` 废除
  - 记忆维护告警改为存储级周带去重，并堵住测试往真库写告警
  - 记忆库最后 4 条 NULL 类型收口：三条定性 + 一条精简保留 + 一条拆包
  - 记忆索引标题的最后一档兜底不再上屏 JSON，改为顶层字段名清单
  - 四条过期记忆全洗：正文只留判据与指路，带日期的快照退回真源
  - 补洗 3 条"轻形态信封"fact，并把"宁可少洗"的尾账关死
  - `fact_type` 为空的存量收口：前缀即类型的补标，说不清的留成清单
  - GPU 采样改成常驻 NVML 会话，新增进程内资源采样中枢 `server/resource_probe/`（消灭"每 5 秒起一个进程"）
  - 存量记忆标签按 v5 判据全量重判（洗存量那一档，2026-10-04）
  - 记忆索引标题不再把元数据信封整坨 JSON 打上屏（`server/memory/manager.py`）
  - 补记（2026-10-05，重启后实测）
  - loop catchup 扫完无条件留一行计数，「没活可干」与「这段代码没跑到」不再同形
  - 占位小时会被回头补跑了，失败收件箱也不再写「错误: unknown」
  - chat_digest：完整群 ID 在通讯录快照里查不到时放行，不再 `exit(1)`
  - 两条"报出来的问题"实测后各修一半：GPU 降级三键补进模板、wx4py 的 notes 与现实对齐
  - 「GitHub 连不上」的口径纠偏：被挡的是端口不是整台主机；本机现场改成一步实测而非文档快照
  - `watchlist.json` 把非持仓标的标成「持仓」：000630 降级为监控
  - `max_db_size_mb` 僵尸配置摘除
  - 两个新镜像仓首同步失败的真因：workflow 侧从不 `git lfs fetch`（UnityPy）+ GitHub 单次 push 2 GiB 上限（Ark-Models）
  - ① mirror-UnityPy
  - ② mirror-Ark-Models
  - `release-backup.yml` 的"每三周"闸门从来没有生效
  - 无 NVIDIA 卡机器的模型准入死锁（`awaiting_first_sample` 永久 503）随采样改造一并解除
  - `daily_summarize` 补显式 `timeout=300`（`server/activity_tracker/loop_actions.py`）
  - `workspace/recorder/tests/test_recorder_gui.py` 的 pyright 存量 5 条清零
  - homework 路由 keywords 补流程动词，实测把三句落空的原话拉回强匹配
  - 脱敏规则「活性」闸门 `tests/release_ci/test_rule_liveness.py`
