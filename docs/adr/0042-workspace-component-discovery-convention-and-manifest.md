# workspace 组件用目录约定 + manifest 双轨发现，主代码库不 import 任何组件

状态：accepted（2026-10-04 追记；机制始于 2026-07-18，manifest 为第二代）

## Context

发布工具要把私有组件（当时是 `workspace/gh_mirror/`，GitHub 仓库备份）从公开分发包里彻底隔离。
只排除它的目录是不够的——`server/` 里有三处**直接 import 它**，于是公开源码树里仍然留有
"存在这么一个组件"的可发现引用（注册名、任务定义、guide 条目）。

同时暴露出一个可维护性问题：每加一个 workspace 组件都要改主代码库的注册点，
而主代码库因此与私有内容产生了编译期耦合。

## Decision

**主代码库只负责"发现"，不负责"知道有谁"**：

1. **约定式扫描**：遍历 `workspace/` 下的一级目录，按约定名找模块并反射取约定符号
   （`register(manager)` / `LOOP_TASK_DEFS` / `GUIDE_REGISTRY_ENTRIES`），
   取不到就跳过，**没有硬编码列表**。
   实现分散在各自的注册表旁边：`server/activity_tracker/loop_actions.py:1592`、
   `server/activity_tracker/loop_manager.py:425`、`server/agent_guide.py:278`。
2. **故意不用 `pkgutil.iter_modules`**：它只枚举带 `__init__.py` 的包，
   而组件目录**按约定不带 `__init__.py`**——所以改用 `_WORKSPACE_DIR.iterdir()` +
   `importlib.import_module` + `ImportError` 静默跳过（理由就写在
   `server/activity_tracker/loop_actions.py:1599-1600` 的注释里，别"顺手优化"回 pkgutil，那会静默发现不了任何组件）。
3. **第二代：manifest 优先，旧约定兜底**。组件可在 `manifest.toml` 声明入口
   （`agent_guide` 段等），`_load_optional_guide_entries()` 先读 manifest
   （`server/agent_guide.py:299-317`），再对未声明的组件回退旧扫描；
   **已通过 manifest 加载的组件跳过旧机制，避免重复注册**。
4. **删除即注销**：删掉 `workspace/<component>/` 后 manifest 消失、扫描跳过，注册点自动空出来。
   ⚠️ 插入点的**数量与清单不在本文写死**（会漂），看运行时：
   `uv run python -m tools.release.cli list-components --audience public`，
   或 grep `load_manifests()` 的消费方。

## Considered Options

- **主代码库里显式 import + 注册表列出组件名**：被拒。私有组件的名字进了主干源码，
  发布扫描要么放过它（泄露可发现引用），要么每轮都得人工删。
- **只在 `.gitignore` / 发布 profile 里排除组件目录**：被拒。目录排掉了，
  主库里的 import 与注册条目还在——这正是当初扫出私有命中的原因。
- **`pkgutil.iter_modules`**：被拒，见上（与"组件目录不带 `__init__.py`"的约定冲突，
  且失败方式是**静默零发现**，比报错坏）。
- **只做 manifest、不留旧约定**：被拒。旧组件已按约定写了 `loop_actions.py`，
  一次性切会逼所有组件同批改，回退路径没有了。

## Consequences

- 正面：主库零私有引用；加/删组件不改主库；发布边界与运行时注册用同一套发现机制。
- 代价 1：**发现失败是静默的**。目录名拼错、缺 `loop_actions.py`、`ImportError` 都会
  表现为"这个组件就是没有 loop 任务"，而不是报错。排查要看 `/health` 的组件段与
  `tools/audit/drift_detector.py`，不能靠"启动没报错"下结论。
- 代价 2：`GUIDE_REGISTRY` **仅在后端启动时合并，没有热重载端点**——新加 workspace 路由
  必须重启后端才生效（这条在 2026-09-14 的交付记录里被实测确认过）。
- 代价 3：双轨意味着**两种真源并存**（`manifest.toml` 与 `loop_actions.py` 里的约定符号）。
  同名组件两边都声明时靠"manifest 优先 + 跳过旧机制"消歧，这个顺序是隐式契约。

## References

- 代码：`server/component_manifest.py`、`server/activity_tracker/loop_actions.py:1592`、
  `server/activity_tracker/loop_manager.py:425`、`server/agent_guide.py:278`
- 策略真源：`docs/release-policy.md` 的「私有组件边界」「组件边界与模块开关」两节
  （清单不在文档里复制，按该文档指示读 `release/audience/public.toml` 或跑 `list-components`）
- 相关 ADR：0034（gh_mirror 清库守护）、0013/0014（发布引擎与 digest 绑定）
- 本 ADR 的起因：复核记忆库 5 条 `architecture_decision` 时发现
  `release_phase2_gh_mirror_isolation` 的正文**写的是 `pkgutil.iter_modules`**，
  与代码注释恰好相反；且它只描述第一代（约定扫描），没提 manifest 已成优先轨。
