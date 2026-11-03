# 环境兼容性与浏览器设置
> 本文档是 `AGENTS.md` 中特定环境约束的详细展开。涉及 OCR/Vision/浏览器操作时需要了解这些约束。

## PaddlePaddle 兼容性

- **PaddlePaddle-GPU 3.2.2**（CUDA 12.6），从官方源 `https://www.paddlepaddle.org.cn/packages/stable/cu126/` 安装（PyPI 仅发布 CPU 版 `paddlepaddle`）
- **PaddleOCR 3.7.0**（含 PP-OCRv6 检测识别模型，相比 PP-OCRv5 检测 +4.6% / 识别 +5.1%）
- **GPU 推理（2026-08-31 实机复测，当前生效配置）**：`config.toml [models] paddle_device = "gpu"`，paddle 3.2.2 (cu126) + RTX 4070 Laptop。PP-OCRv6_medium 实测 GPU：320x96 **0.05s** / 1920x1080 **0.51s** / 2560x1440 **0.76s**；对照组 CPU：0.23s / 3.81s / 5.40s，加速比 **4.6x~7.5x**（冷启动含模型下载约 27s）
- **[已过时 · 勿再据此禁用 GPU]** 2026-08-26 曾记录"GPU 首次推理永不返回"并据此把默认值降为 cpu、推断需升级 paddlepaddle 才能恢复。该结论已于 2026-08-31 推翻：同样的 paddle 3.2.2 / 驱动 Driver API 13.3 / RTX 4070 Laptop，三档各 3 轮共 9 次 GPU 预测全部正常返回，零挂死。成因未复现、无法归因，不排除间歇性；若复发，把 `paddle_device` 改回 `"cpu"` 立即回退
- **无 GPU 环境（如虚拟机）**：`paddlepaddle-gpu` 包照常 import，打印 `You are using GPU version Paddle, but your CUDA device is not set properly` 后自动回退 CPU（`paddle.device.get_device() == 'cpu'`），功能不受影响，**不需要**为 CPU 环境改装 `paddlepaddle` CPU 版。实测 PP-OCRv6_medium 在 CPU 上：模型加载 1.3s（权重已在本地），720×480 图稳态推理 **~1.1s/张**，1704×694 图约 1.5s
- **模型包格式**：paddlex 官方包解包后是 `inference.json` + `inference.pdiparams` + `inference.yml`（Paddle 3.x 的 PIR 格式，**没有** `inference.pdmodel`）。PaddleOCR 2.x 时代的旧 whl 包（`ch_PP-OCRv4_*_infer`，只有 pdmodel/pdiparams、无 yml）**不能**直接给 3.x 用；`*_infer.pth`（PyTorch 权重，如 PDF-Extract-Kit 的 `paddleocr_torch/`）同样不适用。默认 `lang="ch"` 需要三个模型：`PP-OCRv6_medium_det` / `PP-OCRv6_medium_rec` / `PP-LCNet_x1_0_textline_ori`，合计约 146 MB，源 `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/<模型名>_infer.tar`，解包后目录需改名为去掉 `_infer` 后缀的模型名（paddlex 缓存目录读 `<PADDLE_PDX_CACHE_HOME>\official_models\<模型名>\`，见 `docs/model-paths.md`）
- **必须先 import torch 再 import paddle**，否则 Windows 上 `shm.dll` 冲突导致 torch 加载失败（GPU 版同样存在此问题）。后端代码结构天然满足此顺序：`main.py` 不在顶部 import paddle，torch 由 vision/transformers 等模块先加载，paddle 在 `server/ocr.py` 的 `get_ocr()` 中懒加载
- **PIR monkey-patch 已移至 `server/ocr.py` 的 `ModelManager._apply_pir_patch()`**，在首次 OCR 调用时懒加载执行。原本在 `main.py` 顶部同步 import paddlex 会阻塞启动 4.3s，移除后后端启动降至 ~0.6s
- **bbox 坐标**：PaddleOCR v3 的默认 UVDoc 去畸变会改变 UI 截图几何，而输出 bbox 不会逆映射；本服务在 `server/ocr.py` 显式关闭 `use_doc_unwarping`。当前 `config.toml [ocr]` 为恒等变换，网格回归在 800x600、1920x1080、2560x1440 均为 1-3px。升级模型后运行 `tools/debug/verify_bbox_affine.py` 验证；详见 `.agents/wip/ocr_bbox_offset.md`。
- **Computer Use 定位策略**：浏览器优先 DOM locator；桌面/截图文字优先经典 OCR bbox；纯图标/无文字元素用 `understand_image` 描述；这些都失败时才调用 `vision_locate`。远程 VL 默认用于布局、选中状态、遮挡和异常画面描述，不承担普通文字坐标定位。
- **升级门禁**：升级 PaddleOCR/PaddleX、切换 det 模型或更改预处理后，必须确认 `use_doc_unwarping=False`，保持 affine 恒等并跑三档回归。目标为 25/25 匹配、mean≈0、RMS 1-3px、最大误差≤5px。失败先比较直接 detector 与完整 pipeline，禁止先恢复历史 `kx/ky` 参数。
- OCR 模型卸载后重载可能触发 PIR bug，建议保持 `keep_models=true`（默认已开启；通过 ModelLifecycleManager per-model `restore_preload` 配置生效，详见 [Model Lifecycle Manager](#model-lifecycle-manager-显存释放实测) 段）

## 资源采样中枢（常驻 NVML，不创建子进程）

`server/resource_probe/` 是后端进程内**唯一的资源采样 owner**。改动动机：GPU 采样原本是每次查询 fork 一个 `nvidia-smi` 子进程，本机实测：

| 采样方式 | 单次代价 | 每 5s 轮询的日进程创建数 |
|---|---|---|
| `subprocess.run(["nvidia-smi", ...])` | 42–47 ms | ≈ 1.7 万次 |
| 常驻 NVML 会话（`ctypes` 直调） | 首次 `nvmlInit` 17 ms（一次性）+ 单查 0.03 ms | 0 |

消费方（ModelLifecycleManager 的压力状态机、活动采集的 OCR 兜底高负载门）从此只读中枢缓存，自己不起进程。

### ⚠ 适配性：GPU 侧只支持 NVIDIA（务必先读）

**`nvml.dll`（Windows）/ `libnvidia-ml.so.1`（Linux）是 NVIDIA 驱动附带的库，不是本项目安装的依赖。**由此推出三条硬边界：

1. **非 NVIDIA 显卡（AMD / Intel 集显 / Apple 等）上 GPU 探针必然失败**。中枢把它判为 `absent`——语义是"本机没有可采的 NVIDIA 卡"，不是故障：GPU 压力监控随之失效并**放行模型准入**（详见下节），不会阻塞 OCR/MindForge 启动。
2. **不做厂商猜测式降级，也不退回外部命令**（不再有 `nvidia-smi`、`rocm-smi`、`xputop` 之类的子进程兜底）。理由：子进程路线正是本次要消灭的东西；且"哪个命令代表这块卡"的探测本身就是一堆易碎的分支。
3. **要支持别的 GPU，正确做法是新增一个探针实现，不是改 `nvml.py`**（步骤见「扩展点」）。

Linux 上的库名差异已在 `server/resource_probe/nvml.py` 的 `_LIB_CANDIDATES` 里覆盖（`nvml.dll` / `libnvidia-ml.so.1` / `libnvidia-ml.so`，另加 `ctypes.util.find_library("nvml")`）；机器上装了驱动但加载失败时，先按这三条路径自查，再怀疑代码。

### 结构

| 文件 | 职责 |
|---|---|
| `server/resource_probe/nvml.py` | 常驻 NVML 会话：懒初始化、句柄缓存、线程安全；`NvmlUnavailable`（= absent）与 `NvmlQueryError`（= 瞬时）两类异常 |
| `server/resource_probe/probe.py` | `Probe` 协议 + `Sample`；失败分档 `ProbeAbsent` / `ProbeTransient` |
| `server/resource_probe/hub.py` | `SampleHub`：一个 ticker、按探针各自周期采样、缓存最近样本、陈旧度判定、状态迁移事件 |
| `server/resource_probe/config.py` | `[resource_probe]` 段读取（缺段全默认） |
| `server/resource_probe/__init__.py` | `PROBE_REGISTRY` 注册表 + 进程内单例 `get_resource_hub()` |

内置探针（id 即配置键与消费方查询键）：

| 探针 id | 数据源 | 含义 | 主要消费方 |
|---|---|---|---|
| `gpu_mem` | NVML | 整卡显存 used/total（字节） | `model_manager` 的 gpu 压力状态机 |
| `gpu_util` | NVML | 整卡 GPU 利用率 % | 活动采集的 OCR 兜底高负载门 |
| `mem_ram` | psutil | 物理内存 used/total（字节） | `model_manager` 的 cpu 压力状态机 |
| `cpu_percent` | psutil | CPU 占用 %（差分量，不阻塞） | 活动采集的高负载门 |

### 失败分档与新鲜度（这是旧版死锁的修法）

| 中枢状态 | 触发 | 消费方行为 |
|---|---|---|
| `ok` | 采样成功 | 正常推进状态机 |
| `failed` | 探针存在但这次调用抛异常 | 连续累计，由 `PressureMonitor` 按原有阈值转 `PROBE_DEGRADED`（保持降级前保护姿态） |
| `absent` | 抛 `ProbeAbsent`（库加载失败 / 0 设备 / psutil 缺失） | 判"本机无此数据源"→ 监控失效、**准入放行**；按 `absent_retry_sec`（默认 300s）慢速重探，不每周期空转 |
| 无新样本 | 消费方周期比中枢快 | `consume()` 返回 None，**不计入探测失败**（避免误判 `PROBE_DEGRADED`） |

缓存样本超过 `stale_multiplier × interval`（默认 3×）视为过期，`read_cached()` 返回 None；`latest()` 忽略新鲜度供诊断读。

⚠ **`cpu_percent` 是窗口均值，且可能被拖旧**：它由 ticker 每周期读一次 psutil 差分量，代表"上个周期到本周期"的平均占用，不是瞬时值。实测同一套代码两次跑：加压 4 个纯 Python 线程时读数 36.6 / 43.4，闲置窗口读数 0.0 / 27.4。当采样线程被 GIL 竞争拖住时，消费方拿到的可能是**最多 `stale_multiplier × interval` 秒前**的窗口值（不是 0，是旧）——`probe_age_sec` 能看出它多旧。高负载门这类启发式用法可以接受；要做精确归因就别读缓存，直接 `collect()` 并看 age。

### 消费方接入

```python
from server.resource_probe import get_resource_hub, PROBE_GPU_MEM
hub = get_resource_hub()

sample = hub.read_cached(PROBE_GPU_MEM)        # 纯缓存，永不阻塞，可在事件循环上调
sample = await hub.collect(PROBE_GPU_MEM)      # async：缓存新鲜则直接返回，否则放线程池采
sample = hub.collect_sync(PROBE_GPU_MEM)       # 给本就跑在线程里的调用方（会阻塞调用线程）
sample, cursor = hub.consume(PROBE_GPU_MEM, cursor)  # 状态机用：只在有新样本时推进
```

生命周期挂在 `server/core/lifecycle.py`：startup 里先 `ensure_started()` 再起 ModelLifecycleManager，shutdown 里 `stop()`（关 NVML 会话）。消费方发现中枢没跑会**自愈式**就地拉起；拉不起来（`[resource_probe] enabled=false`）则按 `absent` 处理——不允许再出现"永久采不到 → 准入永久 503"。

### 扩展点：加新指标 / 加别的 GPU / 加看门狗

1. **加指标**（磁盘 IO、GPU 温度、单进程 RSS、逐进程显存归因…）：在 `probe.py` 写一个 `Probe` 子类（`sample()` 阻塞式，数据源不存在就抛 `ProbeAbsent`）→ 在 `__init__.py` 的 `PROBE_REGISTRY` 注册一行 → 消费方按新 id 读。配置里可单独开关和设周期：`[resource_probe.probes.<id>] enabled / interval_sec`。**不需要改 hub**。
2. **加别的 GPU**：实现一个走该厂商接口的 `Probe`（AMD 的 Sysfs/AMDSMI、Intel 的 oneAPI Level Zero 等），注册成 `gpu_mem` 的**并列**探针或按机器择一注册；保持"库缺失 → `ProbeAbsent`"的语义即可复用现有的失效放行逻辑。**禁止**在此路径新增子进程采样。
3. **加看门狗**：样本、状态、失败连击、`absent/ok/failed` 迁移事件（`hub.events`）都已集中在中枢，规则引擎只需订阅 `hub.health()` / `hub.status()` / `hub.fail_streak()` / `hub.latest()`，不必自己起第二个采样循环。当前**尚未实现**规则引擎与告警投递，这是有意留下的接入位；接上时注意 `absent` 不该产生告警（那是本机正常形态）。⚠️ 命名撞车：项目里已有的"watchdog 模式"指**屏幕接管授权**（`ControlGrantManager.mode`），与这里的资源占用看门狗无关，别复用那套命名。

### 验证

`uv run python -m pytest tests/llm_vision/test_resource_probe_hub.py -q`——含"把 `subprocess.run` 打成炸弹仍能出样本"的反证用例，以及无卡（absent）准入放行的回归。

## Model Lifecycle Manager 显存释放实测



**Model Lifecycle Manager（`server/model_manager/`）** 是统一管理后端进程内 4 个本地模型生命周期的薄编排层——PaddleOCR（GPU，可逐出）/ 记忆 embedding（CPU，默认钉住）/ guide embedding（CPU，默认钉住）/ MindForge 搜索引擎（CPU，可逐出）。设计文档：`temp/sdd/model-lifecycle-manager/design.md`。

### 显存释放实测锚点（继承 ocr-vram-guard §3）

OCR 增强卸载 = `del + gc.collect() + paddle.device.cuda.empty_cache()`，实测：

| 操作 | 显存变化 | 说明 |
|------|----------|------|
| 加载 PaddleOCR | +484-597 MiB | 稳态占用 |
| 增强卸载 | -434/-597 MiB | 残留 ~163 MiB CUDA context |
| 同进程重载（热） | 稳定 ~1.5-2s | PIR bug 不影响热重载 |
| 冷启动重载 | ~27s | 含模型下载 |

**WDDM 限制**：WDDM 模式下只能查整卡 `memory.used`，无法跨进程归因到具体模型占用。ModelLifecycleManager 的 GPU 监控读 `gpu_mem` 探针（常驻 NVML 会话，见本文档「资源采样中枢」），策略语义是"缓解整卡压力"，不是"谁占了显存"。

### OCR 加载链会带入整套 torch CUDA 镜像（进程级 RAM，且不受逐出管辖）

2026-10-04 单变量实测（新后端起来 4 分钟、`/health`+loops+embedding 均已跑过后采样）：

| 时刻 | 后端 Working Set | 私有提交 | `torch/lib` 家族映射 |
|------|------------------|----------|----------------------|
| 刚重启、未加载 OCR | 429 MB | 844 MB | **0（`torch_python.dll` 不在场）** |
| 只调 `POST /models/ocr/load` 一次 | 1,727 MB | 3,201 MB | **4,012 MB** |
| OCR 继续使用 22 分钟后 | 2,690 MB | 8,309 MB | 4,012 MB |

结论四条：

1. **torch 不在启动导入链上**，是 **OCR 加载那一刻**进来的——触发面在 paddlex 的设备/环境探测
   （`utils/env.py::is_cuda_available()`、`utils/device.py::get_default_device()` 等**函数作用域**的
   `import torch`）。这与本文档「torch 与 paddle 的 import 顺序」记的 `paddlex → modelscope → torch`
   链条是同一条路，那条讲**顺序为什么不能反**，这条讲**它落在进程里值多少**。
2. **`unload` 不 FreeLibrary**：`OcrDriver.unload()` 释放权重与显存，但 torch/paddle 的 DLL 镜像
   一旦调入就常驻到进程结束。实测卸载 OCR 后按文件映射的 torch 家族**一字节没少**，
   进程 Working Set 也无变化。⇒ **重启后端只是临时缓解**，只要再用 OCR 就会回来。
3. **`footprint_mb` 是显存口径，不含这份进程级代价**：`server/model_manager/drivers.py` 里
   `OcrDriver.footprint_mb = 600` 估的是显存稳态占用。看 `/models` 会系统性低估 OCR 对 RAM 的影响，
   想复核进程侧只能用 `workspace/windows_forensics/tools/probes/hw_torch_mapped.py`
   （按文件映射分组；⚠️ 读提权后端必须经 `POST /exec/python`，非提权壳会 AccessDenied）。
4. **卸 OCR 对系统内存的收益是 0**：实测 `dPrivate(-3452 MB) ≈ dVRAM(-3436 MB)`，
   即那 3.4 GB 物理上在 GPU 上、只是被 WDDM 计进提交电荷；系统 RAM 占用率不动。
   ⇒ 想压 RAM，杠杆是"别让设备探测碰 torch"或"把 OCR 推理挪出主进程"，**不是给 RAM 压力加逐出**。

**复现（10-05 01:23，另一次无关重启）**：另一会话为激活 `server/resource_probe/` 再次重启后端，
新进程起来 17.4 分钟、loops/MCP/health 均已服务之后，`torch_python.dll`/`torch_cpu.dll`/
`libpaddle.pyd`/`phi.dll` **仍全部不在场**（WS 729MB、私有提交 1,203MB）⇒ 上面第 1 条两次独立成立，
不是单次巧合。

### 压力监控状态机（每资源独立）

```
NORMAL → REFUSING  （高压持续 ≥high_watermark×high_sustain 个周期，或 ≥critical_watermark 单样本逃生门）
REFUSING → ARMED   （低压持续 ≥low_sustain 个周期）
ARMED → NORMAL     （下次加载成功）
ARMED → REFUSING   （再次飙高）
卸载完成 → NORMAL
特殊态：PAUSED（手动 /models/pause）/ PROBE_DEGRADED（探测失败保持保护）
```

**默认配置**（`[model_manager.gpu]` 段，继承 ocr-vram-guard 全部默认值）：
- `enabled=true` / `high_watermark=0.90` / `high_sustain=3` / `critical_watermark=0.97`
- `low_watermark=0.70` / `low_sustain=6` / `min_loaded_seconds=60` / `unload_timeout_sec=15`

**CPU 监控**（`[model_manager.cpu]` 段）默认关闭（D2 决策：8GB+ 主机内存下 ~2GB 搜索引擎不构成压力，且误杀代价高）。

### 准入与逐出（在压力态下生效）

- **准入门控**：模块加载前调 `manager.admit(model_id)`，按目标资源压力态放行/拒绝。拒绝时抛 `ModelUnavailableError` → 503（调用方应降级处理，不重试）。`PAUSED` 期间放行；手动 load 端点豁免冷却与降级但仍受压力态约束。
- **电平式逐出**：`REFUSING` 期间每采样周期持续评估候选（已加载 + `evictable=True` + 过 `min_loaded_seconds` 保护 + 不在冷却期），按 `(priority, -footprint_mb, reload_cost_sec)` 排序选 victim 串行卸载。无候选 → 维持 `REFUSING` 仅拒绝。当前 GPU 逐出唯一实例就是 OCR。
- **per-model 冷却隔离**：某模型加载失败冷却不牵连同资源其他模型的准入（设计 v2 修订，避免记忆嵌入失败连带拒绝 guide embedding 加载）。连续 ≥3 次失败 → `reload_degraded` 标记（提示重启后端）。

### 无 NVIDIA 卡的机器：GPU 监控判 `absent` 并放行准入

**这是设计行为，不是故障**（判据见 `server/resource_probe/hub.py` 的 `STATUS_ABSENT`）。
虚拟机 / AMD / Intel 集显等机器上 NVML 库不存在或报告 0 设备，`gpu_mem` 探针被中枢判为
`absent` → `PressureMonitor.probe_absent=True` → 状态停在 `normal`、`check_admission()`
直接返回 `probe_absent_no_card` 放行。**文件、密钥、依赖全都对时 OCR 可正常用。**

`/health` 上的可见性：

- `model_manager.probe_absent_resources` 含 `"gpu"` → 该资源没数据源，监控不参与决策
- `GET /models/pressure` 的 `gpu.probe_status = "absent"`，`probe_absent = true`
- 想确认"这台机器到底有没有 NVIDIA 卡"：`uv run python -c "import ctypes; ctypes.CDLL('nvml.dll')"`（Linux 换 `libnvidia-ml.so.1`）能加载即有驱动

**仍然建议 CPU-only 机器把设备显式设成 cpu**——这不是为了绕准入，而是让 OCR 真的跑在 CPU 上：

```toml
[models]
paddle_device = "cpu"        # _active_device="cpu" → OcrDriver.resource="cpu"
```

`paddle_device="cpu"` 是 `config.example.toml` 的模板默认值（模板注明"默认 cpu（稳妥）"），
代码默认 `gpu` 与模板不一致；也是本文档"再出现挂死就改回 cpu"的官方回退开关。
`paddlepaddle-gpu` 在无 CUDA 机器上照常 import 并自动回退 CPU（见本文档开头"无 GPU 环境"条）。
热生效（`get_models_config()` → `load_config()` 带 mtime 缓存，无需重启）。

有 NVIDIA 卡的真机保持 `paddle_device="gpu"` + `[model_manager.gpu] enabled=true`，不要照抄上面。

> 旧版（nvidia-smi 子进程探针）在这类机器上会把"永远采不到首样本"当成待采样，
> 导致 OCR 恒 503 `awaiting_first_sample`，需要靠 `[model_manager.gpu] enabled=false` 重启绕过。
> 现在 `absent` 分支先于 `has_first_sample` 判定，那条死锁不再可能出现
> （回归用例：`tests/llm_vision/test_resource_probe_hub.py::test_admission_allowed_when_no_card_probe_absent`）。

**排查顺序**（下次遇到 503 `model_unavailable`）：先读 `reason` 码——
`awaiting_first_sample` → 探针能出样本但还没出第一个（等下个采样周期，或查 `GET /models/pressure` 的 `probe_age_sec` 是否在涨）；
`probe_degraded` → 有卡但连续采样失败（驱动忙 / CUDA context 异常）；
`probe_absent_no_card` → 不会导致 503，若看到说明该机无 NVIDIA 卡且准入已放行；
`pressure_refusing` → 走下节的 GPU→CPU 条件降级；
`cooldown` / `reload_degraded` → 模型自身状态，等冷却或重启后端。

### GPU→CPU 条件降级（2026-08-31 已实现）

`OcrDriver` 的 `resource` 已从静态 `"gpu"` 改为**动态 property**，跟随 OCR 实际设备
（`server/ocr.py` `ModelManager._decide_device()` 的决策结果 `_active_device`）。

当前行为：

```
GPU 压力拒绝态（gpu monitor REFUSING）时新加载请求到来
  → _decide_device() 现场采样主机内存/CPU（psutil）
    → 可用内存 ≥ gpu_fallback_min_avail_gb(2.0) 且 CPU ≤ gpu_fallback_max_cpu_pct(70)
      → 降级：_active_device = "cpu"，引擎以 CPU 加载，OCR 继续服务
    → 条件不达标（如游戏满载可用内存仅 0.46GB）
      → 维持拒绝（503），宁可不可用也不降级（降级会 swap/OOM 更糟）
```

关键机制（实现约束，改代码前必读）：

- **降级判断必须在加载之前**（GPU 挂死是永不返回而非抛异常，无法 try/except 兜底）
- **GPU 压力信号直接读 gpu monitor 的 `REFUSING` 态**，不能走 `check_admission()`——
  其按 `driver.resource` 选 monitor，未加载/已复位时 resource 为 "cpu" 会恒放行，
  `pressure_refusing` 根本到不了降级判断
- **只对 pressure_refusing 降级**：cooldown / reload_degraded / awaiting_first_sample
  是模型自身状态，换设备解决不了，由 `admit()` 原样拒绝
- **动态 resource 的两个作用**：① GPU 逐出器不再选中已降级的 OCR（否则死循环：
  卸载 CPU 版释放不了显存 → 水位不降 → 继续 refusing → 再降级 → 再被逐出）；
  ② admission 走 cpu monitor（默认 disabled → 放行）
- **不要启用 `[model_manager.cpu] enabled=true`**：启用后降级的 OCR 会走 CPU monitor
  准入，游戏满载 RAM 98% 时照样被拒，降级完全失效。保持关闭让 `check_admission`
  返回 `monitor_disabled` 放行才是期望行为
- **迟滞恢复（防抖）**：降级后至少保持 `gpu_fallback_hold_sec`(300s) 且 GPU 水位回落到
  `low_watermark`(0.70) 以下，下次加载才回 GPU；恢复时机是"下次加载时重新评估"，
  不主动卸载正在运行的引擎。迟滞状态跨卸载保留（防止卸载后水位临界反复切换）
- **卸载后 `_active_device` 复位为 config 期望设备**，下次加载重新决策
- **可观测性**：`/models` 的 `resource` 字段、`/ocr/status` 与 `/health` 的
  `active_device` 字段反映当前实际设备

配置（config.toml `[models]`，阈值依据 2026-08-31 游戏满载实测标定）：

| 键 | 默认 | 说明 |
|---|---|---|
| `gpu_fallback_min_avail_gb` | 2.0 | 降级所需最小可用内存（GB） |
| `gpu_fallback_max_cpu_pct` | 70.0 | 降级允许的最大 CPU 占用（%） |
| `gpu_fallback_hold_sec` | 300.0 | 降级后最短保持秒数（迟滞） |

若要重新验证或调整：**GPU 挂死无法 try/except 兜底**（永不返回，不是抛异常），
所以降级判断必须发生在**加载前**。实时监控压力态：`GET /models/pressure`
（判断降级是否触发要读 `recent_events`，`refusing` 态可能只持续亚秒级）。

### 实测验证（2026-08-31，游戏满载 8 分钟）

采集器 `temp/monitor_gpu_pressure.py`，原始数据 `temp/gpu_pressure_experiment_20260831.jsonl`
（159 样本 / 480s，含 GPU 水位、state、OCR 存亡、CPU%、内存）。

| 时间 | GPU 水位 | 事件 | OCR | 可用内存 |
|---|---|---|---|---|
| 01:45:34 | 44.4% | 游戏启动 | ✓ | 14.54 GB |
| 01:48:03 | 90.4% | 首次破线 | ✓ | ~0.6 GB |
| 01:49:02 | **90.10%** | `normal → refusing`，cause=`high_sustain` | ✓ | — |
| 01:49:02 | — | `refusing → normal`，cause=`eviction_complete`（**间隔仅 0.38s**） | — | — |
| 01:49:06 | 90.1% | — | **✗ 已卸载** | 0.81 GB |
| 01:49:48 | 73.5% | 卸载释放约 1.35 GB | ✗ | — |

结论：

- **后端压力逻辑确实会触发**，行为与设计一致（`high_sustain=3` × `poll_interval=5s`）
- **确认只有卸载、没有降级**：OCR 从 `loaded_ids` 中消失，直接不可用
- ⚠️ **`refusing` 态只持续 0.38 秒**，2 秒轮询**完全采不到**（脚本日志里 `state` 一路 `normal`）。
  判断有没有触发**必须读 `recent_events`，不能只看 `state` 字段**

### CPU 降级可行性（实测判定）

按"内存够 + CPU 压力不高"两个条件实测：

| 条件 | 实测值 | 判定 |
|---|---|---|
| CPU 压力不高 | 中位 23.0%，峰值 47.7% | ✓ 达标 |
| 内存够 | **最低可用 0.46 GB**（峰值占用 98.5%） | ✗ **不达标** |

**结论：游戏满载时瓶颈是主机内存，不是 CPU。** 可用内存仅 0.46 GB，
CPU 模式的 OCR 加载会直接触发 swap 甚至 OOM，比"不可用"更糟。
因此降级条件必须是"可用内存 ≥ 阈值"且阈值真正生效（按本次数据，余量至少 2 GB 以上）。
已按此实现条件降级（见上文「GPU→CPU 条件降级」），不达标时维持拒绝。

> 另注：后端 CPU 监控默认关闭（`get_model_manager_config()` 兜底为 gpu=True / cpu=False），
> `/models/pressure` 返回 `cpu.ratio=None`、`samples_taken=0`。这是**有意为之**：
> 降级后的 OCR 归属 cpu monitor，若启用且 RAM 高压会照样拒绝降级模型，降级完全失效。
> "CPU 压力不高"条件由 `server/ocr.py` 的 `_can_fallback_to_cpu()` 用 psutil 现场采样，
> 不依赖常驻监控循环（降级是低频事件）。

### 控制面端点（全部纳入审批分类）

新控制面 `/models/*` 端点已在 `server/route_tags.py` 的 `_APPROVAL_POST_PATTERNS` 增 `/models` 前缀 + `_MODERATE_EXCLUDE_PATTERNS` 镜像，**所有 POST 端点判为 `approval_required`**（避免 fail-open 默认让 agent 无审批卸载 OCR 瘫痪 Computer Use 主路径）：

| 端点 | 审批级别 | 用途 |
|------|----------|------|
| `GET /models` | read_only | 列出全部注册模型状态 |
| `POST /models/{id}/load` | approval_required | 手动加载模型（豁免冷却/降级，仍受压力约束） |
| `POST /models/{id}/unload` | approval_required | 手动卸载模型 |
| `POST /models/pause` | approval_required | 暂停压力监控（手动超驰通道） |
| `POST /models/resume` | approval_required | 恢复压力监控 |
| `GET /models/pressure` | read_only | 各资源压力态 + used/total + 最近迁移事件 |

兼容：`/ocr/models/*` 与 `/mindforge/preload|unload` 保留并委托管理器（调用方零改动）；`/ocr/status` 与 `/health.ocr` 追加 `mlm_state` 摘要。

### keep_models 写回链

原 OCR 与 MindForge 的 `keep_models` 死旋钮已统一迁移到 ModelLifecycleManager per-model `restore_preload` 配置（语义所有权迁移，单例所有权不变）：

- **写入口**：`/ocr/models/keep`（OCR）/ MindForge 同款旋钮 → 写入 `[model_manager.models.<id>].restore_preload`
- **读出口**：`/health.ocr`、`/ocr/status`、`/mindforge/status`、GUI `client/panels/llm_pool.py` → 全部回读管理器生效值
- 配置位置：`[model_manager.models.ocr]` 段（覆盖默认值）

### Health 聚合

`/health.model_manager` 低频枚举（不放高频计数器避免 client 指纹漂移）：

```json
{
  "enabled": true,
  "gpu_state": "NORMAL",       // NORMAL/REFUSING/ARMED/PAUSED/PROBE_DEGRADED
  "cpu_state": "disabled",      // 默认关闭
  "loaded_ids": ["memory_embedding", "ocr"],   // 当前已加载的 model_id 排序列表
  "reload_degraded_ids": [],   // 连续失败 ≥3 次的 model_id 列表（提示重启后端）
  "last_transition_ts": 1234567890.0   // 最近一次状态机迁移时间戳
}
```

`HealthResponse` 已显式声明 `model_manager` 字段（schema `extra='forbid'` 强制，避免漏写触发 schema 错误）。

### MindForge 搜索引擎已知局限

- 搜索端点持局部引用（mindforge.py），`unload()` 只删属性，1-2GB 实际要等**在途搜索结束**才释放；无 OCR 那样的 `forced` + 延迟补释放语义
- `load()` 无参协议与 `get_searcher(index_dir)` 必填参数冲突的解决：`unload()` 前自留 `last_index_dir` 快照（`unload()` 会清空 `_index_dir`），`load()` 优先用快照；从未加载过则复刻 `/mindforge/preload` 解析逻辑（`_resolve_mindforge_dir()` + meta.json 存在性检查）

### 记忆 embedding 手动卸载后果（默认钉住规避）

- 默认 `evictable=False`（钉住）：记忆 embedding 在每个 MCP 请求路径上，逐出后必然立即被下一请求拉回形成慢循环，且卸载窗口内写入的消息**永久缺失向量索引**——逐出收益为负
- 手动卸载（走审批门槛）后记忆语义检索**静默降级为 BM25-only**（无 503，消费者都先查 `ready`，不会写入零向量）
- **恢复步骤**：`POST /models/memory_embedding/load` + `POST /memory/rebuild_vector_index`（补卸载窗口缺口）

## 远程 VL（ModelScope API-Inference）

本地 VL（Qwen2.5-VL-3B + `envs/vl/`）已于 2026-07-07 下线，原因：8 GB VRAM 显存不足 + 3B 模型质量不达标（空表格/understand 503）+ 维护成本高。设计思路与删除环境清单见 `.agents/wip/local_vl_decommissioned.md`。

当前 VL 能力由远程 API 提供：
- **默认 provider**：魔搭社区 ModelScope（`https://api-inference.modelscope.cn/v1`）
- **默认模型不是常量，且随 use_case 变方向**（Ticket 03，2026-10-11）：`server/llm_pool/key_store.py` 取该 key 里 `vl` scope 中 tier 在 `use_case.default_tier` 范围内的启用模型，**遍历方向按 use_case 分策略**——`vl_ocr` / `vl_activity_tracker` 取 **tier 最低**（便宜优先，高频截图分析），`vl_vision` 取 **tier 最高**（质量优先，定位坐标会直接变成点击位置，错的比贵的更糟）。同 tier 按 `models` 数组顺序。方向由 `model_order_for_use_case()` 决定（只有 `vl_vision` 在降序白名单里，未注册 use_case 一律升序，与改造前逐字一致）。运行时以 `GET /vision/status` 的 `vl_model` 为准，别照抄文档里的模型名
- **魔搭会下架模型 ID**：下架后调用返回 `400 {"error":{"message":"Model id : X , has no provider supported"}}`（官方文档明示"旧模型可能下线不再支持，请配置当前支持的模型 ID"）。2026-09-19 实测 `Qwen/Qwen3-VL-8B-Instruct` 与 `Qwen/Qwen3-VL-235B-A22B-Instruct` 均已失效。**换模型 = 改 keys.json 的 tier/enabled，不改代码**
- **`has no provider supported` 与 `Invalid provider: X` 是两种错**：前者表示该模型在魔搭没有任何推理提供方绑定（下架，加后缀也救不回来）；后者表示 `<Model-Id>:<ProviderName>` 后缀里的提供方名非法（如 `:DashScope` 是合法名）。外部提供方路线见 `docs/model-service/API-Inference/api-provider`
- **OpenAI 兼容 chat completions API**，图像以 JPEG base64 data URL 内嵌 messages content
- **RPM ≈ 5**，`max_concurrency=1` 序列化调用避免触发限流；**429/5xx** 触发 cooldown（默认 60s，可通过 keys.json 中 VL key 的 `vision.rate_limit_cooldown` 配置）+ 退避重试 (2, 4, 8, 16, 32, 60) 秒（可通过 `[vision] vl_retry_backoffs` 配置）
- **400/404/422 归"永久失败"，与 429/5xx 分家**（2026-10-05 补，此前 400 被当瞬时错误：同一个已下架模型被打满 6 档退避才换 key）。分类按状态码不按子串（`classify_permanent_model_error`），失败响应体截 150 字进日志（`VLHTTPError.body`），否则排查时只剩 `HTTP Error 400: Bad Request` 猜不出原因。永久失败的模型被排除后 `resolve_keys(use_case, exclude_models={key_id: {model}})` 会解析出**同一 key 的下一个** `vl` 模型；全部模型都判永久则提前停止，不把退避跑满。排除**跨调用**有效：判死时登记 `(key_id, model)` 级今日失效标记（`_dead_models_today`），当天后续调用直接跳过，次日 00:00 整表解除（给上游补回同名 ID 一次重新证明的机会）；**要让下架模型永久不被选中仍要改 keys.json 的 tier/enabled**（上一条）。状态按 `(key_id, model)` 而执行锁与并发上限仍按 key/token 级（同 token 串行不放松），这个故意不一致见 ADR 0044
- **多 provider 自动 failover**：通过 `key_store.resolve_keys(use_case)` 路由（`vl_ocr`/`vl_vision`/`vl_activity_tracker`），按 tier 升序遍历候选 key，第一个不在冷却中且并发未满的胜出；全部冷却时返回下次可用时间
- **配置（v9）**：VL provider 参数（`base_url`/`api_key`/`model`/`max_concurrency`/`timeout`/`rate_limit_cooldown`）统一存放在 `data/llm/keys.json` 中含 `vl` scope model 的 key 记录的 `vision` 段；`config.toml [vision]` 段只保留全局开关和编码参数。新增 VL provider：在 keys.json 中加一条含 `vl` scope model 的 key，并附 `vision` 段即可
- **禁用**：`vl_enabled=false` 可完全关闭远程 VL
- **模块**：`server/vl/remote_vl.py`（`RemoteVLClient` 单例，接口与旧 `QwenVLBridge` 兼容）

## Florence2 兼容性

- transformers 版本锁定 `>=4.45,<4.47`（5.x 和 4.57.x 不兼容）
- Florence2 processor 从 `Florence-2-base` 加载（仅 `tools/image_organizer/` 工具使用，从 HF cache 加载；OmniParser 移除后主后端不再加载 Florence2）
- 主 `.venv` 仍含 `transformers`（embeddings 模块用）和 `torch`（embeddings + STT 用），Florence2 processor 可正常加载

## 管理员权限

- 键鼠操控需要管理员权限，否则 Windows UIPI 阻止 `SetCursorPos`
- **用户手动启动**：`start.bat` 自动检测并 UAC 提权，启动前杀掉占用 8766 端口的旧进程
- **agent 自启动**：一律用非提权命令 `.venv\Scripts\python.exe -m server.main`，不运行 `start.bat`、不替用户点 UAC；非提权后端的 MCP/REST/浏览器/截图/OCR 正常，只有键鼠与 UIA 激活窗口受限
- `/screen/status` 的 `admin_privileges` 字段指示当前权限状态

## Playwright 浏览器操作

- **Playwright 已安装**：`playwright` 已在 .venv 中，**禁止执行 `pip install playwright` 或 `playwright install`**
- **连接方式**：所有浏览器操作必须通过 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行的调试浏览器实例，**禁止启动新浏览器**
- **反检测：不要注入任何 JS 伪装补丁**。本项目连的是用户自启的**真实有头 Chrome**，
  指纹天然合规；`playwright-stealth` 之类的补丁库是为「Playwright 自己 launch 的无头
  Chromium」设计的，在本项目场景下是负资产（实测详见 `docs/browser-anti-detection.md`）。
  该依赖已于 2026-09-03 从 `server/`、全部 workspace 脚本中移除。
- **标准模式**：
  ```python
  from playwright.async_api import async_playwright

  async with async_playwright() as p:
      browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
      context = browser.contexts[0]
      page = await context.new_page()
      # ... 操作页面（无需任何 stealth 包装）
  ```
- **为什么不加补丁**（一句话版）：伪装要做**减法**。真实浏览器上 `navigator.webdriver`
  本来就是 `false`、`plugins` 原生合规，加补丁只会把 `languages` 从 `zh-CN` 改成
  `en-US`（与 HTTP 头 `Accept-Language: zh-CN` 矛盾）、把 WebGL renderer 从真实 GPU
  改成 `Intel Iris`（与其他硬件信号矛盾），并且会**永久污染用户 tab 的后续导航**。
- **下载行为（2026-09-03 实测，CDP 关键坑）**：`connect_over_cdp` 连真实 Chrome 后
  **从不自动配置下载行为**——浏览器一切下载默认落盘用户系统下载目录（实测多轮 e2e 在
  `<data_drive>:/<system_data_root><data_drive>:/Downloads` 累积泄漏 10 个 `test_download*.txt`，12 字节）。
  两条相关事实：
  1. `Download.save_as` **拦不住先落盘**：本地小文件瞬时下载完成，Chrome 已先写入默认
     目录，`save_as` 只是另存一份（对照实验：沙箱清空重跑仍泄漏 + 沙箱 0 文件）。
     `Download.cancel()` 能拦（阻止落盘），但只对"无 download_dir 的 wait_for"路径生效。
  2. **`Browser.setDownloadBehavior` 不能加"已配置就跳过"的幂等短路**：Playwright 的
     `Download.save_as` 在 CDP 模式下会自己重设 downloadPath，首次配置只对第一次下载
     生效，之后又回落默认目录（实测幂等版第二次会话必漏 1 个）。
- **下载沙箱根治模式**：每次 `create_session` 拿到 page 后**强制重发**（勿幂等）：
  ```python
  dl_dir = os.path.join(PROJECT_ROOT, "temp", "browser_downloads")
  os.makedirs(dl_dir, exist_ok=True)
  cdp = await ctx.new_cdp_session(page)
  await cdp.send("Browser.setDownloadBehavior", {
      "behavior": "allow", "downloadPath": dl_dir, "eventsEnabled": True})
  ```
  实测：去幂等每次强制重发后，连续两次 download 会话 + e2e_full 全量 110 用例，
  Downloads 泄漏 0/0/0。实现见 `server/browser/session/manager.py` `_configure_download_behavior`。
  另：`wait_for(download)` 在未提供 `download_dir` 时应 `download.cancel()` 而非放任挂起。
- **操作原则**：`page.evaluate` 只读取DOM数据，`mouse.move + mouse.click` 执行交互操作（模拟人类）
- **已有工具脚本**：`workspace/bilibili_gacha/bilibili_gacha.py`、`workspace/arknights_gacha/arknights_gacha.py`、`workspace/endfield_gacha/endfield_gacha.py` 等都使用此模式，参考它们而不是从零开始

## 调试浏览器实例（通用，支持 Chromium 内核）

> ⛔ **profile 数据安全另见 `docs/browser-profile-safety.md`（2026-10-24 立）**：`chrome_debug/` 是用户真实数据，
> 跨浏览器打开会触发扩展 MAC 校验失败并**静默删除扩展文件**；动之前先 `tools/browser/profile_guard.py backup`。
> 本节只讲「怎么起、怎么回收」，不讲「怎么不把数据搞坏」——那在安全文档里。

**项目有专用的调试浏览器实例，agent 应自主启动和管理，不要问用户。** 支持任何 Chromium 内核浏览器（Chrome / Edge / Brave / Vivaldi 等），脚本会自动探测已安装的浏览器。

> ⚠️ **修正（2026-09-19，VM 实测）：上面"agent 应自主启动、不要问用户"在本项目的 VM 上不成立。**
> 经 agent 工具通道启动的 GUI 进程**活不过那条命令结束**——三种写法全被回收：
> `Popen` 裸起、`creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`、
> 再加 `CREATE_BREAKAWAY_FROM_JOB`；改用 `run_in_background` 长驻任务托管能撑住
> （实测 CDP 连续存活 1140s），但**任务一结束照样被收**（保活循环跑满即 9222 消失）。
> 结论：**VM 上调试浏览器必须由人工常驻**（用户终端 / 桌面快捷方式），agent 启动后顺手验一下
> `GET http://127.0.0.1:9222/json/version` 就干活，**不要承诺"我起了它就在"**。
> 宿主机是否同样受限**未实测**。判据提醒：`netstat` 看到 9222 有监听 ≠ 下一条命令里它还活着。

> ⚠️ **再修正（2026-09-30，宿主机实测）：宿主机同样受限，根因 = harness 的会话/任务清理。**
> WorkBuddy / Qoder 等 harness 用自身 Bash（含 `run_in_background`）拉起的调试浏览器，
> 会在**对话/后台任务结束时被自动回收**——2026-09-30 实测：`run_in_background` 拉起后当时
> 一切正常（扫描都能跑），随后 harness 清理后台任务，9222 消失、<data_drive>:/<bilibili_videos>登录流程被打断。
> **结论（铁律）：调试浏览器一律用后端终端拉起**——`exec_terminal_spawn`（进程挂在 8766
> 后端名下，不受 harness 会话清理影响）。**禁止**用 harness 侧 Bash / `run_in_background`
> 启动浏览器。验证方式照旧：`browser_status` 或 `GET /json/version`。

> ⚠️ **补丁（2026-10-24，宿主机实测）：光"浏览器走后端"不够，后端自己也不能挂 harness。**
> 把后端本身用 harness 的 Bash / `run_in_background` 拉起（`(python -m server.main &)`）时，
> 会话清理会把**后端**一起硬杀：日志在某一秒戛然而止、**没有任何 shutdown / close_all 记录**，
> 随后 shutdown 钩子（或进程树）带走全部 session page，最后一个 tab 关掉时 Chrome 整体退出，
> 9222 随之消失——现象与"浏览器神秘死亡"一模一样，真因却在上游。实测：17:53 起的后端、
> 18:35 用 `/terminals/spawn` 起的 Chrome 都正常，18:43 后端日志断在一行 loop 上，9222 同时消失。
> **判据**：① 后端日志末行是否 shutdown；② 父进程链——后端父应是 `cmd.exe` ← `taskeng/svchost`
> （计划任务），而不是 harness 的 bash/cmd。**冷启动脱离 harness 的办法**：一次性计划任务
> 跑一个引导 bat（`@echo off` / `cd /d <项目根>` / `.venv\Scripts\python.exe -m server.main >> logs\…`），
> `schtasks /Create /SC ONCE /ST 23:59 /F` → `/Run` → 起好后 `/Delete`（进程不受删除影响）。
> 另：启动浏览器用 `cmd_list` 数组（`["<venv python>","tools/browser/start_debug_browser.py"]`）
> 就能绕开 cmd.exe 引号陷阱，**不需要写临时 bat 文件**——写 bat 再让终端跑 bat，是没查 `cmd_list` 才走的弯路。

> 📋 **后端空闲回收机制（2026-10-10 查证 `server/browser/session/manager.py`，必要管理而非故障）**：
> SessionManager 对浏览器 session 有**静默租约回收**：有主 session 900s（owner 每次操作
> 自动续租）、anonymous 300s（没人续租，更快收），后台每 60s 扫一遍，回收时
> `close_page=True` **连 tab 一起关**。参数在 config.toml `[browser]`
> `session_idle_timeout_secs` / `session_anon_idle_timeout_secs` / `session_max_count`
> （当前 config.toml 未配 `[browser]` 段，全走 `server/config.py` 默认值）。
> **连带效应：被回收的 tab 若是窗口里最后一个，Chrome 进程整体退出，9222 随之消失。**
> 因此「9222 没了」**先查回收证据，不要叫"神秘死亡"也不要盲目重启**：
> 1. 墓碑查询：对已回收 session 的操作会 404，`debug_detail` 自带
>    `已于 HH:MM:SS 被关闭：idle_timeout / lru_evicted_anon / manual:<caller>，owner=…，url=…`；
> 2. 后端日志搜 `idle 超时已清理` / `LRU 淘汰`；
> 3. 长任务保活 = 让 owner session **周期性有操作**（每次操作即续租），而不是想办法关掉回收。
> 另：后端 shutdown 会 `close_all()`（关全部 session page + 断开 CDP）——后端重启时
> 调试浏览器里的任务页同样会被收，长任务别横跨后端重启。

- **启动脚本**：`tools/browser/start_debug_browser.py`（旧名 `start_debug_chrome.py` 作为薄包装保留）
- **启动命令**：
  - 默认（非交互，空白配置）：`uv run python tools/browser/start_debug_browser.py`
  - 复制登录态：`uv run python tools/browser/start_debug_browser.py --copy-user-data`（建议先关闭源浏览器）
  - 显式指定浏览器：`uv run python tools/browser/start_debug_browser.py --browser-path "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"`（本机默认即 Chrome，通常**不需要**传；只有要临时换 Edge/Brave 时才用）
  - 自定义端口/目录：`--port 9223 --user-data-dir /path/to/dir`
- **工作原理**：
  1. 检查 9222 端口是否已有浏览器在监听
  2. 默认非交互：若 `chrome_debug/` 不存在则创建空目录（空白配置）；`--copy-user-data` 时从源浏览器（按 browser_path 推断）复制用户数据（保留登录态和扩展）
  3. 以 `--remote-debugging-port=9222 --user-data-dir=chrome_debug` 启动独立浏览器实例
  4. 等待就绪后返回
- **浏览器探测优先级**：未指定 `--browser-path` 时，按 Chrome → Edge → Brave → Vivaldi 顺序探测默认安装路径
- **浏览器选型：优先 Chrome（2026-10-24 重测，撤回 09-05 结论）**
  - **旧结论已失效**：09-05 那条「生产配对 Edge 152 / `chrome_debug/` 是 Edge 谱系 / 未钉 `browser_path` 时 Chrome 启动即闪退，必须 `--browser-path` 指向 Edge」**在本机不成立，已撤回**。实机核对：Edge 只有 `138.0.3351.109` 一个版本目录（2025-07-30 安装，从未有过 152），Chrome 也不是「旧版 135」而是 `140.0.7339.208`（比 Edge 新）。当初那次闪退是**降级方向**（旧版浏览器读新版 profile）的表现，被误写成「必须用 Edge」这条生态结论，之后一路被照抄。
  - **2026-10-24 实测（升级方向安全）**：用 `Chrome 140.0.7339.208` 打开由 `Edge 138.0.3351.109` 写入的同一个 `chrome_debug/`，**正常启动无闪退**，`GET /json/version` 返回 `Chrome/140.0.7339.208`，CDP `contexts[0].cookies()` 读到 **308 条 cookie / 99 个域**（297 条有值，`bilibili` 17、`feishu` 8、`xiaohongshu` 8、`modelscope` 7 全在）⇒ 跨浏览器共用一个 user-data-dir 在这条方向上是安全的，登录态没有丢。
  - **判定方法（换浏览器/跨版本后照这个验，不要照抄任何人写死的结论）**：① 起一次，`curl 127.0.0.1:9222/json/version` 看 `Browser` 字段是不是你要的那个；② 连 CDP 数 `contexts[0].cookies()` 的**条数 + 域分布 + 非空值比例**，和迁移前比；③ 只有**降级方向**（旧版浏览器打开新版 profile）才会闪退，表现为打出 `DevTools listening` 后立刻 `exit 3`。
  - **已钉配置**：`config.toml [chrome] browser_path = "C:\Program Files\Google\Chrome\Application\chrome.exe"`，让自动探测的 Chrome 优先变成显式声明，不随安装情况漂移。`BROWSER_CANDIDATES` 本来就是 Chrome 优先，以后**默认不要传 `--browser-path` 指 Edge**。
  - ⚠ **但「安全」只对数据成立，扩展会被清空（2026-10-24 实测代价）**：Chrome 140 首次打开这份 Edge 138 写入的 profile 后，`Default/Extensions/` 下 13 个扩展目录**只剩空壳**（无 `manifest.json`），`Secure Preferences` 里保留 29 条 `extensions.settings` 但 `path` 为空，目录 mtime `18:38:03` 正是 Chrome 启动之后。原因：扩展条目的 MAC 由**各 profile 自己的 seed** 签名，别的浏览器写的条目校验不过 ⇒ 判定篡改 ⇒ 删文件、留残条。cookie（308 条）与 History（48MB）不受影响。**结论：切浏览器可以，但要按「扩展需要重新注册一次」来预估，别以为拷了 profile 就全都带过去。**
  - **Chrome 137+ 稳定版已忽略 `--load-extension`（同日实测）**：参数确实进了命令行（`chrome://version` 可见），但扩展不加载（`/json/list` 只有内置扩展、`chrome://extensions` 卡片数 0）。所以「给调试实例离线装扩展」不能靠启动开关。
  - **离线装扩展唯一稳路**：`chrome://extensions` → 开右上「开发者模式」→「加载已解压的扩展程序」→ 选**自己解包出来的目录**（2026-10-24 裁决：扩展源码不再入库，原 `tools/browser/extensions/` 已删；需要时从 CRX 解包到仓库之外，如 `%LOCALAPPDATA%\Parcae\extensions\`，别放进仓库——第三方扩展自带的 `.html` 会撞 C3 硬规则、4MB 源码也只是体积负担）。这一步**一次即可持久**（注册写进 profile，重启不掉），代价是启动时顶部有「请停用以开发者模式运行的扩展」提示。⛔ 纯拷文件到 `Default/Extensions/` **不生效**（同上 MAC 校验），必须让它自己注册一次。**ID 是否保留取决于 manifest 有无 `key` 字段**：有 `key`（如 KeePassXC-Browser 1.10.3）⇒ ID 仍是官方值 ⇒ 依赖官方 ID 的原生消息宿主白名单能对上；无 `key` ⇒ ID 由路径派生，会连不上 native host。
- **与工作浏览器隔离**：调试浏览器使用独立的 `chrome_debug/` 用户数据目录（可在 config.toml `[browser] user_data_dir` 配置），**绝对不会影响用户正在使用的工作浏览器**
- **自主操作流程**：
  1. 用 `browser_status` 检查调试浏览器是否已运行
  2. 如果未运行：**用后端终端 `exec_terminal_spawn`** 执行 `tools/browser/start_debug_browser.py`
     启动（进程挂在后端名下，活过 harness 会话；**禁止 harness 侧 Bash / `run_in_background`
     拉起**，那会在对话结束时被自动回收，见本节开头 2026-09-30 修正）
  3. 如果启动失败：提醒用户检查 Chromium 内核浏览器是否安装，或用 `--browser-path` 显式指定
  4. **不要问用户"要我来开还是你来开"**——直接开（**VM 上例外**：agent 起的进程活不过命令
     结束，见本节开头 2026-09-19 修正，那种情况要让用户人工常驻）
  5. **不要打开用户的工作浏览器**——只用调试实例

## 端口保留区（WinNAT / Hyper-V 动态保留，会让服务"绑不上端口"）

> **症状**：服务启动失败，报 `WinError 10013`（`[Errno 13] Permission denied`）；端口明明没被占用、也没有权限问题。
> **本机已发生三次**：本地后端 8766、adb server 5037、调试浏览器 9222。

**机制**：Hyper-V / WinNAT 会从**动态端口范围**里划走若干"保留块"，块内端口**任何进程都绑不上**——注意报的是 `10013`（访问权限不允许），**不是**"已被占用"的 `10048`，所以极易误判成权限/防火墙问题。

**判据（可复用，按顺序读）**：

```bash
netsh int ipv4 show excludedportrange protocol=tcp          # ① 保留区清单
python -c "import socket; socket.socket().bind(('127.0.0.1',<PORT>))"   # ② 直接探针：10013=被保留
netsh int ipv4 show dynamicport tcp                          # ③ 目标端口落在哪段动态范围
```

- 保留区条目**带 `*`** = 持久保留（注册表 `...\Tcpip\Parameters\PersistentPortExclusions`），**停 WinNAT 没用**；**不带 `*`** = WinNAT 动态保留，⛔ **每次开机重新随机分配** ⇒ 「昨天能起、今天起不来」是正常现象，不是代码问题。
- ⛔ **别用「端口有没有被占用」判**：`netstat` 里查不到 ≠ 能绑上。
- 对照探针很重要：再绑一个同类高端口，若它 OK ⇒ 排除权限/防火墙通病，锁定"这个端口被保留"。

**⛔ 本机真凶（2026-10-12 查清，解释了"为什么以前从没出过这事"）**：

`HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\MaxUserPort = 15000`

Windows 默认动态端口范围是 **49152–65535**；`MaxUserPort` 把它压成 **1024–15000**（`netsh int ipv4 show dynamicport tcp` 显示「启动端口 1024 / 端口数 13977」，13977 = 15000−1024+1，完全吻合）。WinNAT 只能从动态范围里划块，范围被压到低位后，划出的块就落到低位 ⇒ 撞上 5037、8766 这类常用端口。

**Windows 默认配置下 WinNAT 的块全在 49152 以上，5037 / 8766 永远不会有事。** 所以这不是"突然坏了"，是**一直在雷区边上走**，看每次开机划到哪儿。同键下另有 `ReservedPorts`（本机实测 `{1433-1434}`）。

**已发生的三次**：

| 端口 | 服务 | 表现 | 处理 |
|---|---|---|---|
| **8766** | 本地后端 | `[Errno 13] ... winerror 10013` | `tools/disk_io_audit/fix_port_and_start.py`（`net stop winnat` → 起后端 → `net start winnat`）；完整判据见 `tools/disk_io_audit/README.md` |
| **5037** | adb server | MAA 报 `could not read ok from ADB Server` / `failed to start daemon` | 换端口 23456，启动器 `<data_drive>:/<green_apps_root>\MAA-v5.17.2-win-x64\StartMAA-adb23456.bat` |
| **9222** | 调试浏览器（CDP） | ⚠️ **表现与上两条不同**：没有 `WinError` 异常栈，chrome 主进程与子进程都活着、`netstat` 查不到监听，`start_debug_browser.py` 只报「调试端口未就绪」并 `exit 1` | 换未保留端口（2026-10-19 实测 **7000** 一次成功）。真因只在 chrome 自己的 stderr：`net\socket\tcp_socket_win.cc bind() returned an error: 0x271D (WSAEACCES)` + `Cannot start http server for devtools`。⛔ 别去杀进程 / 查占用——那不是"被占用" |

> **9222 不再是无条件可用的约定端口**：`docs/browser-anti-detection.md` 与各 skill 的 `connect_over_cdp("http://127.0.0.1:9222")` 仍然正确，但**起浏览器那一步要先确认 9222 不在保留区**（`netsh int ipv4 show excludedportrange protocol=tcp`；2026-10-19 本机 `9208-9307` 这一段就吃掉了 9222）。
> ⚠️ `tools/browser/start_debug_browser.py` 把 Popen 的 stdout/stderr 丢进 `DEVNULL`，**真因被掩盖**；诊断时自己 `subprocess` 起一次并把 stderr 落到文件，否则只能看到"未就绪"。

**已内置自动顺延（2026-10-19，本机第三次复发后落地）**

后端启动时**在 uvicorn 之前**自己试绑端口（`lib/port_binding.py`），撞到 10013 就按序往后找，并且**整段跳过保留区**（读 `netsh ... show excludedportrange`；本机保留段首尾相连到 9807，逐 1 顺延永远跳不出去）。跳段不占尝试次数配额。全部失败才 `exit 2` 并打印诊断命令。

结果写进 `data/runtime/server_port.json`（`preferred_port` / `port` / `pid` / `degraded` / `reason` / `status`），**任何消费方都不该再自己拼端口**：

- 查询：`GET /runtime/port`、`GET /runtime/consumers`
- 代码内：`from lib.runtime_endpoint import resolve_base_url`（客户端 / 后端自环 / exec 子进程的 `PARCAE_API` 都走它）
- 下游登记：`data/runtime/consumers.json`，端口变更时自动把 `127.0.0.1:<旧端口>` 改成新端口（改前备份 `.bak_portrewrite`、只匹配回环 URL、无匹配不写）

⛔ **IDE 的 MCP 客户端不读这个声明**，它只认自己配置里的 URL；自动改写只覆盖"已登记且内容里确实出现旧端口"的项，改完仍需**重连 IDE** 才生效。

⚠ 排查时别只看 `data/logs/server.log`：uvicorn 自己的 bind 报错走 stderr，历史上正是因此表现为"启动完成后莫名关闭"。现在预探测发生在 uvicorn 之前，端口问题一定会进文件日志。

**对策一：换端口 —— 选大于 `MaxUserPort` 的端口（推荐，零管理员、零重启）**

端口只要**不在动态范围内**，WinNAT 就抢不到，一劳永逸。本机动态范围上界是 15000 ⇒ **选 16385 / 20000 / 23456 这类**。低端口里只有 **>6126** 才安全（2026-10-12 实测保留区含 `4859-5258`、`5357`、`5426-6126`，所以 5555、6001 同样中招；10000 与 16384 则分别被占用/是模拟器端口）。

adb 的换法（零改码、零系统改动）：

```bash
ANDROID_ADB_SERVER_PORT=23456 adb.exe start-server
ANDROID_ADB_SERVER_PORT=23456 adb.exe connect 127.0.0.1:16384 && \
ANDROID_ADB_SERVER_PORT=23456 adb.exe devices
```

- ⛔ **变量名是 `ANDROID_ADB_SERVER_PORT`，不是 `ADB_SERVER_PORT`** —— 实测 `adb.exe` 二进制里两个字符串都有，但**只有前者生效**（2026-10-12 按二进制字符串猜错、实测无效后更正）。
- 客户端与被它拉起的 server 共用该变量；子进程会继承（`CreateProcessW` 第 7 参传 `nullptr`）。
- ⚠️ 给 MAA 换端口**必须走启动器**（bat 内 `set` 后启 `MAA.exe`）：直接双击 `MAA.exe` 不带该变量时仍只找 5037，照样失败。
- ⚠️ 边界：只解 MAA 自己；MuMu 若另有依赖 5037 的部分不受影响（本次未发现）。

**对策二：临时解堵（只治这一次）**

```
net stop winnat  →  启动目标服务  →  net start winnat
```

需管理员；期间网络/WSL 会短暂中断几秒；⛔ **重启后仍会复发**（`MaxUserPort` 没改）。

**对策三：根治（需管理员 + 重启；动手前先查清 `MaxUserPort` 是谁设的）**

```bash
netsh int ipv4 set dynamicport tcp start=49152 num=16384
```

改回 Windows 默认范围后，WinNAT 划块回到高位。⚠️ `MaxUserPort=15000` 可能是某个软件的要求（历史遗留 / 软件优化），**改前先确认来源**，别在没查清前提动系统参数。

### 改完必须重启，且要防着被改回去（2026-10-19 实测）

`netsh ... set dynamicport` 改的是**动态端口范围**，而 WinNAT 的保留块是**每次开机按当时的范围重新划分**的，
已经划走的那批块不会因为改范围而立刻释放。本机实测：改完 `show dynamicport tcp` 已是 `49152 / 16384`，
但 `show excludedportrange` 仍占着 `8708–9807`，后端照样顺延到 9808 ⇒ **不重启等于没改**。

⚠️ 这类值最容易被「网络优化 / 游戏加速器 / 系统精简脚本」在装机或清理时写回低位，而症状要等到
**下一次开机正好撞上保留块**才出现（表现是"昨天还能起，今天一启动就关"，且日志里一条报错都没有）。
现在有三层可见性，不用等故障才发现：

| 层 | 怎么查 | 说明 |
|---|---|---|
| 启动体检 | `data/logs/server.log` 搜 `[parcae.port] WARNING` | 每次启动都查；**只有真会让端口绑不上的配置才 WARNING**（动态端口起点 < 49152、首选端口落在保留区） |
| 运行时查询 | `GET /runtime/port` 的 `port_env` 字段 | 含 `start/count/end/max_user_port/problems/notes/healthy`，下游（管家前端）可据此提示 |
| 改动留痕 | `python -m workspace.disk_manager.settings_watch.watch diff --item tcp_port_range` | 纳入快照后能 diff 出值被改；`restore` 默认干跑，`--apply` 才写 HKLM（需管理员） |

`problems` 与 `notes` **分两档，不要合并**：前者计入 `healthy` 并打 WARNING，后者只记录（日志 DEBUG、`/runtime/port` 可见）。\
遗留 `MaxUserPort` 属后者 —— Vista+ 已由 `dynamicport` 接管，本机 `netsh` 报 `49152/16384` 与
`MaxUserPort=15000` 并存即证明它不参与端口分配。把它计入 `problems` 会让 `healthy` 恒为 false、
每次启动都 WARNING，真正的风险反而被稀释。

`MaxUserPort` 与 `DynamicPortRange*` 的关系：Vista+ 以 `dynamicport` 为准，`MaxUserPort` 是 XP 时代遗留项。
⚠️ Windows 对**默认值不落注册表** —— 设成 49152/16384 之后 `DynamicPortRangeStartPort` 反而查不到，
这是正常的（等于默认 ⇒ 删值），不要当成异常去"修"。

## 网络与环境查询技巧

本节是跨任务可复用的环境经验（不局限于 OCR）。

### 国内网络查 HuggingFace 的正确姿势

- `huggingface.co` 直连**必超时**（`WinError 10060`）
- `hf-mirror.com` 用裸 urllib 请求 API **返回 403**
- 唯一稳的姿势：用项目 venv 里的 `huggingface_hub` 并显式指定 endpoint

```python
from huggingface_hub import HfApi
files = HfApi(endpoint="https://hf-mirror.com").list_repo_files("<repo_id>")
```

这样能拿到含 `1_Pooling/`、`modules.json` 之类子路径的完整文件清单，
可用来验证 `allow_patterns` / `ignore_patterns` 到底能不能匹配到目标文件。

⚠️ **但这条姿势只覆盖元数据面，不覆盖文件下载面**——见下一节实测。

### 模型权重下载的路由矩阵（2026-09-21 实测）

要拉 HuggingFace / Zenodo / GitHub 上的模型权重时，**先按下表选路，不要逐个试**。

| 源 | 状态 | 症状 |
|---|---|---|
| PyPI / `files.pythonhosted.org` | ✅ 通 | 正常解析与下载 |
| `huggingface.co` | ❌ | 直连超时（2026-10-05 复测 `curl --max-time 15` → `000`；hosts 已清空，所以不再是本机黑洞） |
| `hf-mirror.com` **元数据面**（`/api/v1/...`） | ✅ 通 | `HfApi(endpoint=...)` 的 `list_repo_files` / `repo_info(files_metadata=True)` 正常返回，**能列文件清单和体积** |
| `hf-mirror.com` **数据面**（`/…/resolve/main/<file>`） | ❌ **ConnectTimeout** | GET 与 HEAD 均超时（40–60s），**不是慢，是连不上** |
| `zenodo.org` | ❌ | `getaddrinfo` errno 11004；`www.zenodo.org` 能被 `gethostbyname` 解出真实 IP，但 httpx 连接仍失败 |
| GitHub（2026-10-05 复测，**按主机分辨，不再是"全域黑洞"**） | `api.github.com` ✅ REST 200 / `objects.githubusercontent.com` ✅（404=可达） / `codeload.github.com` ✅ TCP 0.05s / **`github.com` ❌ TCP 直连超时 ⇒ `git push`/`fetch` 失败** / `raw.githubusercontent.com` ❌ TCP 连上但 TLS 被 reset | 详见下节「GitHub / HuggingFace 的连通性判据」 |
| **`gh-proxy.com`** | ✅ **可交付文件内容** | 2026-10-05 复测：`gh-proxy.com/https://raw.githubusercontent.com/git/git/master/README.md` 返回 **200**（`raw.githubusercontent.com` 直连被 TLS reset，套一层就通） |
| **`modelscope.cn`** | ✅ **数据面可用** | `https://www.modelscope.cn/models/<ns>/<name>/resolve/master/<file>` 实测 200 |
| `musescore.org` 主站 | ✅ 通 | `/en/download` 返回 200；但 `download.musescore.org` / `api.musescore.org` **DNS 失败** |
| 本地代理 | ❌ 无 | 11 个常见代理端口全关，注册表 `ProxyEnable` 未设 |

**结论性判据**：`hf-mirror` 能列清单 ≠ 能下权重。看到"镜像站可用"就动手写下载逻辑，
会卡在数据面超时上白等一轮。**优先 `modelscope.cn` 直下，其次 `gh-proxy.com` 套 GitHub 原始链接，
最后才考虑让用户手动下载拷贝。**

### 系统 ISO / 大文件的镜像选源（2026-09-28 实测）

和上面"元数据面 ≠ 数据面"是同一个教训的另一种形态：**镜像站列得出来，不代表下得快、甚至不代表下得动**。
拉 Linux ISO 这类 GB 级文件前，先花 30 秒做 range 探测，别凭印象选源。

```bash
# 单段 8MB range 探测，读 speed_download；整包开下前先测
curl.exe -s --max-time 25 -r 100000000-108388607 -o probe.bin \
  -w "code=%{http_code} bytes=%{size_download} speed=%{speed_download}B/s\n" \
  "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/<版本>/<文件>.iso"
```

| 源 | 该次实测 | 备注 |
|---|---|---|
| `mirrors.tuna.tsinghua.edu.cn` | 最快 | 选它 |
| `mirror.nju.edu.cn` / `mirrors.ustc.edu.cn` | 次之 / 更慢 | 都能连，只是慢 |
| `mirrors.aliyun.com/ubuntu-releases/` | ❌ 假可用 | 只回 **302** 跳到 `http://iso-osm.mirrors.aliyuncs.com`，而该主机直连 `code=000` 完全不通；不加 `-L` 时看着"有响应"，实际一个字节没拿到 |

两条固定做法（避免"下完才发现坏"）：

- **先存校验基线再下载**：ISO 同目录有 `SHA256SUMS` / `SHA256SUMS.gpg`，下载前先抓到 `temp/`，
  这样校验用的和下载的是同一份，不用下完再去别处取数字。
- **下到 `.part`，校验通过才改名**：`-o xxx.iso.part` 配 `-C -` 续传，`certutil -hashfile … SHA256`
  比对无误后再 `mv` 成正式文件名。中断不会留下"看起来像完整 ISO"的半成品。

⚠️ `curl.exe` 的 `-o` 目标路径必须写**字面 `F:/` / `F:\`**，写 msys 风格 `/f/...` 会被静默重定位到 `C:\f\...`，
命令报成功但文件不在你以为的位置。

### GitHub / HuggingFace 的连通性判据（本机状态会翻面，按探针读，别抄快照）

**第一步永远是读本机的两个现场**，因为这两处会变、且都会把"网络不通"伪装成别的形状：

```bash
# ① hosts 有没有 GitHub/HF 条目、几条、什么时候被改的
uv run python -c "import os,datetime;p=r'C:\Windows\System32\drivers\etc\hosts';\
raw=open(p,encoding='utf-8',errors='replace').read();L=raw.splitlines();\
print(os.path.getsize(p),'bytes |',datetime.datetime.fromtimestamp(os.path.getmtime(p)),'|',\
sum('github' in l.lower() for l in L),'github 行')"
# ② 443 是不是被本机某进程占着（加速器/代理会接管 TLS）
netstat -ano | grep ":443 .*LISTENING"   # 拿到 PID 再 tasklist /FI "PID eq <pid>"
```

两次实测就是"会翻面"的证据：**2026-10-05** hosts 只有 5 字节、零条 GitHub 条目（09-17 那套 26 条黑洞已在
2026-10-04 23:11 被清空）；**2026-10-06 15:20** `Steam++.Accelerator.exe` 把 hosts 写回 69 行 / 2214 字节，
其中 27 条把 `github.com`、`api.github.com`、`huggingface.co` 等指向 `127.0.0.1`，自己监听 `0.0.0.0:443`。
后一种状态下 `connect(127.0.0.1, 443)` **瞬间成功**，但 `curl https://api.github.com/zen` 回
`CRYPT_E_NO_REVOCATION_CHECK`（TLS 被本地终结），所以"443 连上了"≠"GitHub 通了"，
更不等于可以往外推凭据。

分辨到"到底是哪一层挡着"，用这三步，每步一个独立仪器：

| 探针 | 2026-10-05 结果（hosts 干净那次） | 判读 |
|---|---|---|
| `getaddrinfo` | `github.com`→`20.205.243.166`、`api.github.com`→`.168`、`codeload.github.com`→`.165` | 走真 DNS，不是黑洞 |
| 裸 TCP，**分端口**打同一个 IP | `.166:443` **8s 超时**，但 `.166:22` **0.2s 连上**；`.168`/`.165` 的 443 **0.05s 连上** | 挡住的是**那台 edge IP 的 443 端口**，不是整台主机、不是 DNS、不是代理、不是本机防火墙 |
| `--resolve github.com:443:.165/.168` 打 git 端点 | 一个回 **400**、一个回 **403**（真 `github.com` 回 `000` 超时） | 相邻 IP 不代管 `github.com` 的 git vhost，**换 IP 推不动**，别把凭据往那儿发 |

> ⚠️ 上一版本表里写的是"`.166` 这个 edge IP 不通"，那是**没分端口**测出来的错口径：同一 IP 的 22 端口当时
> 0.2s 就握手成功（`ssh.github.com:443` 也通）。被挡的只有 443。写"整 IP 不通"会让人误判成
> "换 SSH 也没戏"，实际 `ssh.github.com:443` 那条路是通的。

- **`git push` / `git fetch` 到 `https://github.com/...` 会在 connect 阶段失败**（`Failed to connect to github.com port 443`），
  而同一时刻 `api.github.com` 的 REST 完全正常——所以"能查 GitHub"不等于"能推 GitHub"，两者走不同 IP。
  遇到这个先跑上面两组确认是"本机在挡"还是"出口在挡"，再决定等网络或换出口；
  **不要改 hosts、不要改 DNS、不要给 git 塞代理**——要动这些得用户明示。
- 历史背景（供查旧账）：2026-09-17 前后本机用 hosts 主动黑洞 GitHub 全家与 `huggingface.co`，
  当时 `gh-proxy.com` 是唯一读路径；`gh-proxy.com` / `modelscope.cn` 作为**下载**路线至今可用（见上文路由矩阵）。
- **不要为了跑通任务去改 hosts 或系统 DNS**——那是机器策略，突破需用户明示。

**顺带一条通用教训**：`socket.gethostbyname()` 能解出 IP **不代表能连上**。
本例 `www.zenodo.org` 解出 188.185.43.153，但 httpx 连接失败。
判"可达"必须用真实请求（带超时的 GET/HEAD），不能只查解析。

### api.github.com REST：用 `curl --resolve` 绕过 hosts 黑洞（2026-10-17 实测）

上一节说「`--resolve` 打 git 端点推不动」是对的，但**别把它推广成"GitHub 全挂"**——
`api.github.com` 的 **REST 面**可以用 `--resolve` 完整跑通，这是 hosts 被 Steam++ 写黑时
唯一不用改任何系统配置的只读通道。

```bash
# ① nslookup 不读 hosts，能拿到真值（getaddrinfo 会读到 127.0.0.1）
nslookup api.github.com          # → 20.205.243.168
# ② 把域名钉到真 IP
curl -s --resolve api.github.com:443:20.205.243.168 -H "User-Agent: parcae" \
  "https://api.github.com/repos/<owner>/<repo>/git/trees/<branch>?recursive=1"
```

实测一次拉到 14004 个文件的子树、多个目录各自 recursive 均 200。三个坑：

- **响应在 ~2MB 处会被传输层截断**，症状是 `Unterminated string starting at: ...`，
  **不是** GitHub 的 `truncated` 标记。判据：JSON 解析失败就退化为**非递归逐层下钻**，
  别当成偶发失败去重试（同一请求再发一次常常就直接成功了）。
- 路径可能含裸控制字符（`[uc]lua` 这类目录名），`json.loads(txt, strict=False)` 才解得了。
- 未认证限流 **60 次/小时**：按顶层目录分别 `recursive=1` 合并着拉，别逐目录递归展开。

### 夸克网盘分享页的清单抓取（2026-10-17 实测）

`pan.quark.cn/s/<pwd_id>` 是 SPA，列表要走 PC 端接口，**无需登录**：

```bash
# ① 取 stoken（唯一需要 POST 的一步）
curl -s -X POST "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token" \
  -H "Content-Type: application/json" -H "Referer: https://pan.quark.cn/s/<pwd_id>" \
  --data-binary '{"pwd_id":"<pwd_id>","passcode":""}'
# ② 分页列目录：pdir_fid=0 是根，子目录用条目的 fid 继续
curl -s "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail?pwd_id=<pwd_id>\
&stoken=<stoken>&pdir_fid=0&_page=1&_size=100&_fetch_total=1&_sort=file_type:asc,file_name:asc"
```

token 返回里 `data.title` 是分享标题、`data.file_num` 是**顶层**条目数——**先看这两个字段再决定
要不要展开**。这两项就能直接回答"这个分享是什么"，能省掉几千次请求。

⛔ **坑：GET 请求千万别带 `Content-Type`**。带了会返回 `400 code=14001 "非法token"`，
看着像 stoken 失效，实际是 header 的问题。只有 POST 那一步需要 Content-Type。

目录树可能又深又碎（实测 797 个目录 / 8 层，每个目录只 2~6 项），要**分层并发**遍历，
否则会先撞上自己设的请求上限，误以为抓完了。并发度先问用户——第三方站点有封 IP 风险。

### 查 PaddlePaddle 可用 wheel 版本

GPU 版**只发在官方索引**，PyPI 上只有 CPU 版：

```
https://www.paddlepaddle.org.cn/packages/stable/<cu118|cu126|cu129|cu130>/paddlepaddle-gpu/
```

直接抓目录页用正则 `paddlepaddle_gpu-(\d+\.\d+\.\d+)-cp3XX-cp3XX-win_amd64\.whl` 提取最准。

⚠️ **GitHub tag 上有 ≠ 已发 wheel**：例如 3.4.0 有 tag，但四个 CUDA 索引全都没有 wheel。
判断"装的版本过不过时"要分三层看：PyPI 最新 / 官方索引 wheel 实际上限 / GitHub tag，
**取 wheel 实际上限才是真正可升级的目标**。

### 测"可能挂死"的推理调用

怀疑某个调用永不返回（如 GPU 推理挂死）时：

- **不要用主进程直接跑**。放独立脚本用 `timeout N <cmd>` 跑，靠退出码判断：
  **124 = 超时挂死，0 = 正常返回**
- **分层探测**定位挂死层级：① 框架基础算子（如 `paddle.matmul` 打到 `gpu:0`）
  → ② 完整 pipeline 首次推理 → ③ 多档分辨率多轮稳定性。
  **① 通过不能说明 ③ 没问题**
- 验证"历史 bug 已修复"要有铁证：`git show HEAD:<file>` 导出旧版到临时文件，
  用 `importlib.util.spec_from_file_location` 动态加载，新旧两版跑同一用例对比**调用计数**，
  而不是改完代码就宣称修好了。

### 确认后端是否真的在用 GPU

`/health` 接口不暴露 device 字段。最硬的证据是查 GPU 计算进程列表：

```bash
nvidia-smi --query-compute-apps=pid --format=csv,noheader
```

**后端 PID 出现在这个列表里**才说明真的在用 GPU —— CPU 模式下后端进程不会进该列表。
配合显存占用前后对比（OCR 上 GPU 约 +500 MiB）一起看。

### 采样 `/health` 的 `last_inference_ms`

调完 `/ocr/base64` **立刻**读 `/health` 会拿到**上一次**调用的值（实测偏差可达 2 倍以上）。
隔几秒再读才准。正确值应与端到端耗时接近（差十几毫秒是 HTTP 开销）。

### torch 与 paddle 的 import 顺序

必须先 `import torch` 再 `import paddle` / `import paddleocr`，否则 Windows 上 `shm.dll`
冲突导致 torch 加载失败（`WinError 127`）。
注意 `import paddleocr` 会走 paddlex → modelscope → torch 链，
所以实际约束是 **torch 先于 paddleocr**。

> 这条链还有进程级代价：它发生在 **OCR 加载那一刻**（paddlex 设备探测里的惰性 import），
> 会把整套 torch CUDA 镜像（实测按文件映射 4,012MB）带进后端且 `unload` 不释放。
> 见本文档「Model Lifecycle Manager」章 →「OCR 加载链会带入整套 torch CUDA 镜像」。

### GitHub 仓库克隆被限速到 ~15 KiB/s（用 gh-proxy 前缀绕过）

实测（2026-09-17）：本机 `github.com` 的 **git 协议 pack 传输**被限速到 **~15 KiB/s** ——
171MB 的仓库 5 分钟只下到 4.3MB（7%），按此速率要 3 小时以上。

**先排除误判**：同一时刻别的站点都正常（PyPI 4.0 MB/s、Cloudflare 615 KB/s），
说明不是本地线路问题；`api.github.com` 的元数据请求也是瞬时返回
（**限速只作用于大流量 pack 传输**，不影响 REST API）。所以"API 通、clone 慢"并不矛盾。

**绕过**：给 URL 套 `https://gh-proxy.com/` 前缀，同一仓库实测 **~3.8 MB/s（46 秒克隆完）**：

```bash
git clone --bare --progress \
  "https://gh-proxy.com/https://github.com/<owner>/<repo>" <dir>
```

- 镜像站可用性会变：本例 `gh-proxy.com` 可用，`ghfast.top` / `ghproxy.net` 均返回 403。
- **只用于读（clone / fetch）**：push 必须直连 `github.com`（要带凭据，镜像站不代理私有仓写入）。
- 测速时注意：本例中 `curl -o /dev/null` 的写入会被沙箱拒绝（表现为 `size=0` / `curl: (23)`），
  **必须落盘到真实文件**才能拿到可信的 `speed_download` —— 否则会误判成"所有站点都 0 B/s"。

#### 补充（2026-10-12 复测）：证书是**另一个独立故障**，两个都要治

**⛔ 证书问题：本机 git 的 `openssl` 后端认不了 GitHub 证书**

本机 `<data_drive>:/<apps_root>/Git/etc/gitconfig` 是 `http.sslbackend=openssl` + `http.sslcainfo=<git 自带 ca-bundle.crt>`，直连 GitHub 报：

```
fatal: unable to access 'https://github.com/...': SSL certificate problem: unable to get local issuer certificate
```

**`schannel` 后端（走 Windows 证书库）可以**。它与"限速"是**两个互相独立的故障**：只治限速不改后端，会卡在证书上；只治证书不加镜像，会限速到断流。

**零持久化注入法（git 2.31+ 环境变量，子进程全继承，不改任何全局配置）**：

```bash
GIT_CONFIG_COUNT=2
GIT_CONFIG_KEY_0=http.sslBackend                 GIT_CONFIG_VALUE_0=schannel
GIT_CONFIG_KEY_1=url.https://gh-proxy.com/https://github.com/.insteadOf
                                                 GIT_CONFIG_VALUE_1=https://github.com/
git fetch --no-tags --depth 1 upstream "refs/tags/<tag>:refs/tags/<tag>"
```

命令行等价：`git -c http.sslBackend=schannel -c url."https://gh-proxy.com/https://github.com/".insteadOf=https://github.com/ fetch ...`

**复测数字（2026-10-12）**：

| 走法 | 结果 |
|---|---|
| 直连 `git fetch` 单个 tag | ❌ 9 分钟只到 **63%**（17.5 MiB / ~35 KiB/s），随后 `error: RPC failed; curl 56 Recv failure: Connection was reset` + `fatal: early EOF` |
| 直连 codeload 抓 8MB range | 102 KB/s |
| gh-proxy 抓同样 8MB range | **218 KB/s** |
| **经 gh-proxy + schannel `fetch --depth 1`** | ✅ **23.5 秒**落地 |

⚠️ 本机 2026-09 曾测到 gh-proxy **3.8 MB/s**，本次只有 **218 KB/s** —— **加速器也在退化，别拿旧速度估时间**。

**只拉单个 tag，别全量**：`git fetch --no-tags upstream "refs/tags/<tag>:refs/tags/<tag>" --depth 1` 秒级且失败原因可见；全量 `--tags` 在本机曾要 **~15 分钟**，且配 `2>$null` 会把失败原因吞掉，导致误报"tag 不存在"（真实原因是限速/证书/超时）。

#### 补充（2026-10-15 复测）：gh-proxy 已 403，改走 ghfast.top；且 **Range 不可用**

**⛔ 镜像站会翻面，别抄上一轮的名字**。2026-10-15 复测：`gh-proxy.com` 对 **git 端点与 codeload 都返回 403**（此前 09-17 是 3.8 MB/s、10-12 是 218 KB/s，一路退化到不可用）。

**当日实测数字**（同一仓库 `zai-org/ZCode` 的 85 MiB 快照，15–30 秒短探针）：

| 走法 | 结果 |
|---|---|
| `git clone --depth 1` 直连 github.com + schannel | **~78 KB/s**（3 分钟只到 14 MiB） |
| `codeload.github.com` 直连 | **~30 KB/s** |
| `github.com/<o>/<r>/archive/<ref>.tar.gz` 直连 | **~65–80 KB/s** |
| **`https://ghfast.top/` 前缀 + archive tarball** | ✅ **~690 KB/s**（85 MiB 用 60 秒；另一仓库 32 MiB 用 23 秒） |
| `ghproxy.net` 前缀 | 57 KB/s（能通但没优势） |
| `hub.gitmirror.com` / `gh.llkk.cc` / `github.moeyy.xyz` | ❌ `code=000` 连不上 |

- **证书仍是独立故障**：`git` 直连必须 `-c http.sslBackend=schannel`（见上一节）；curl 加 `--ssl-no-revoke`，否则 CRL/OCSP 检查超时伪装成"网站不通"。
- **⛔ 该链路不支持 HTTP Range**：`-r 0-N` 拿到的是 `200` + 整个文件（不是 206），`-C -` 续传直接 `rc=33 CURLE_RANGE_ERROR`。
  ⇒ **不能做多连接分段并行**，只能「单连接 + 长 `--max-time` + 换镜像重试」。脚本里写续传循环会原地空转 30 次然后失败。
- **⛔ `tar` 的 `F:` 冒号陷阱**：msys 的 `tar` 把 `F:/...` 解析成「远程主机 `F` + rsh」，报
  `tar (child): Cannot connect to F: resolve failed`。**archive 路径与 `-C` 目标都必须写成 msys 风格 `/f/...`**。
  注意这不通用：`curl.exe -o` 反而**必须**写字面 `F:/`（见上文），两者要求相反。
- **快照（tarball）vs git 仓库怎么选**：只要 `--depth 1`，两者价值几乎等价（都没有历史）。
  按 `git clone` 78 KB/s vs `ghfast` 690 KB/s 的差距，**默认走 ghfast + tarball**，落地后补一份 `SOURCE.md` 记 commit 与拉取方式即可。

**⚠️ 拉之前先看仓库体积**：`gh api repos/<owner>/<repo> --jq '{size_kb:.size,default_branch}'`
（`api.github.com` 不受影响，秒回）。例：`openclaw/openclaw` 实测 **9.4 GB**，整体拉取不可行；
这种「只要一两个文件」的场景改用 contents API 单文件回源，2 次调用即可拿到 ★ 级证据：

```bash
gh api repos/<owner>/<repo>/git/trees/main?recursive=1 --jq '.tree[].path' | grep 'agent-loop\.ts'
gh api repos/<owner>/<repo>/contents/<path>?ref=main --jq .content | base64 -d > agent-loop.ts
```

**⚠️ 被中断的 fetch 会留下僵尸锁**：`.git/shallow.lock`，后续操作报
`fatal: Unable to create '...shallow.lock': File exists`。确认没有 git 进程在跑后删掉即可。

### 超时杀不掉的 git 子进程

`subprocess.run(["git", ...], timeout=N)` 在 Windows 上可能**杀不干净**：git 会派生
`git-remote-https` / `git-index-pack` 等孙进程并继承 stdout/stderr 管道；父进程被 kill 后
`communicate()` 仍阻塞在管道上等待最后一个持有者退出 ——
症状是**脚本 CPU 近 0、超时时间早已过去但进程不退出、日志停在最后一行**。

- 排查：`Get-Process git,python` 看有没有「启动时间 = 最后一条日志时间」、CPU 极低的残留进程。
- 处理：按 PID 逐个 `Stop-Process -Force`（**别误杀后端 python 进程**，用启动时间区分），再清理工作目录。

### curl 抓 HTTPS 报 000 / 退出码 35 ⇒ 先加 `--ssl-no-revoke`

本机走 schannel 后端时，`curl` 会去查证书吊销列表（CRL/OCSP），国内网络下这一步常超时 ⇒ 表现为
**HTTP 000 / 退出码 35（SSL connect error）**，看着像"网站不通"，其实是**吊销检查被卡住**，不是 TLS 握不上。

- 先加 `--ssl-no-revoke` 重试；能通就确认是吊销检查问题。
- 与"限速"是**两个互相独立的故障**（见上文「GitHub 仓库克隆被限速」）：只治限速不改后端会卡在证书上，只治证书不加镜像会限速到断流。

### 查"模型/资源存不存在"（免鉴权入口）

- OpenRouter 全量模型清单：`https://openrouter.ai/api/v1/models`（**免鉴权**，直接 curl）。
- HuggingFace 按作者列模型：`https://huggingface.co/api/models?author=<org>`。
- ⚠ 列表里的日期是**上架/建库日**，不是训练或发版日，别拿它做版本判断。
- HF 许可证读 API 返回的 `tags` 里的 `license:X`，**别去猜页面文本**；配校验：repo 名与记录里的参数规模 token 取**交集，空则拒写**。

### 抓站点数据：先找结构化负载，别用"grep 命中数"判断有无数据

按顺序试，三条都各自救过场：

1. 先翻 `<script type="application/ld+json">`——不少站点把结构化数据直接挂在页面里。
2. **页面很大却 grep 0 命中** ⇒ 数据在 Next.js 的 flight 流里：搜 `self.__next_f.push`。
3. **付费 API 返 401 时先 curl 对应页面**——页面本身可能公开，只是 API 要鉴权。

⛔ 数据可能被转义、分包或延迟注入，**命中 0 ≠ 没数据**，别据此下"页面没有该数据"的结论。

### 别人送来的改动：先查"声称了但没做"

拿到他人（或 AI）提交的改动，先做一次低成本核对：
`grep -rn "<对方声称的函数名>" --include="*.py" src/`；README / commit 注释里的承诺同理。
补完后要配**反向验证锚**——造一个真会抛的输入，确认捕获真的生效，否则那是一段死代码。
（与上文「验证历史 bug 已修复要有铁证」是一组：一个查"做了没做"，一个查"改对了没"。）

### 找"找不到的素材"别只搜工作树

按顺序找：① 同级的压缩包（`*.7z` / `*.zip`）；② 被 `.gitignore` 排除的目录；③ 用户后补的文件。
⚠ 文档里写的**绝对路径属于写它的那台机器**，在本机解析不到 **≠** 该素材不可得。
