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
- **默认模型不是常量**：`_pick_model_for_tier`（`server/llm_pool/key_store.py`）取该 key 里 `vl` scope 中 **tier 最低**的启用模型，同 tier 则按 `models` 数组顺序取第一个。运行时以 `GET /vision/status` 的 `vl_model` 为准，别照抄文档里的模型名
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
- `start.bat` 自动检测并 UAC 提权，启动前杀掉占用 8766 端口的旧进程
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

- **启动脚本**：`tools/browser/start_debug_browser.py`（旧名 `start_debug_chrome.py` 作为薄包装保留）
- **启动命令**：
  - 默认（非交互，空白配置）：`uv run python tools/browser/start_debug_browser.py`
  - 复制登录态：`uv run python tools/browser/start_debug_browser.py --copy-user-data`（建议先关闭源浏览器）
  - 显式指定浏览器：`uv run python tools/browser/start_debug_browser.py --browser-path "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"`
  - 自定义端口/目录：`--port 9223 --user-data-dir /path/to/dir`
- **工作原理**：
  1. 检查 9222 端口是否已有浏览器在监听
  2. 默认非交互：若 `chrome_debug/` 不存在则创建空目录（空白配置）；`--copy-user-data` 时从源浏览器（按 browser_path 推断）复制用户数据（保留登录态和扩展）
  3. 以 `--remote-debugging-port=9222 --user-data-dir=chrome_debug` 启动独立浏览器实例
  4. 等待就绪后返回
- **浏览器探测优先级**：未指定 `--browser-path` 时，按 Chrome → Edge → Brave → Vivaldi 顺序探测默认安装路径
- **移植配置与浏览器版本的兼容性（2026-09-05 实测）**：本机调试浏览器生产配对是 **Edge 152**（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；用户目录下另有旧版 Chrome 135）。新版浏览器写入的用户数据目录，旧版 Chrome 启动即闪退（能打出 `DevTools listening` 但立刻 exit 3）。配置目录跨浏览器/跨版本移植后必须用目标浏览器实测；`chrome_debug/` 为 Edge 谱系配置，config.toml 未钉 `browser_path` 时自动探测会选中 Chrome 导致闪退，需显式 `--browser-path` 指向 Edge
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

### 超时杀不掉的 git 子进程

`subprocess.run(["git", ...], timeout=N)` 在 Windows 上可能**杀不干净**：git 会派生
`git-remote-https` / `git-index-pack` 等孙进程并继承 stdout/stderr 管道；父进程被 kill 后
`communicate()` 仍阻塞在管道上等待最后一个持有者退出 ——
症状是**脚本 CPU 近 0、超时时间早已过去但进程不退出、日志停在最后一行**。

- 排查：`Get-Process git,python` 看有没有「启动时间 = 最后一条日志时间」、CPU 极低的残留进程。
- 处理：按 PID 逐个 `Stop-Process -Force`（**别误杀后端 python 进程**，用启动时间区分），再清理工作目录。
