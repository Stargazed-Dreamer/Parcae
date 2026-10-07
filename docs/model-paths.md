# 模型路径管理

## 设计目标

所有 AI 模型路径通过 `config.toml` 的 `[models]` 段统一管理，避免硬编码绝对路径。
发布时只需改一个配置项（`external_dir = ""`），所有模块自动回退到项目内 `weights/` 目录，
无需搜索替换。

## 配置方式

### 个人部署（开发环境）

```toml
# config.toml
[models]
external_dir = "<data_drive>:\\ai_models"
```

各模块自动使用以下子目录：

| 模块 | 路径 | 代码位置 |
|------|------|----------|
| PaddleOCR | `<data_drive>:\ai_models\paddleocr`（paddlex 实际读取 `<该路径>\official_models\<模型名>\`） | `server/ocr.py` |
| 嵌入模型 | `<data_drive>:\ai_models\embeddings` | `server/memory/config.py` → `server/memory/embeddings.py` |
| Whisper STT | `<data_drive>:\ai_models\faster_whisper` | 非本项目，仅共享存储 |
| CosyVoice TTS | `<data_drive>:\ai_models\cosyvoice` | 非本项目，仅共享存储 |
| HF 缓存 | `<data_drive>:\ai_models\huggingface` | 环境变量 `HF_HOME` |
| ModelScope 缓存 | `<data_drive>:\ai_models\modelscope` | 环境变量 `MODELSCOPE_CACHE` |

### 发布模式

```toml
# config.toml
[models]
external_dir = ""
```

各模块回退到项目内 `weights/` 目录：

| 模块 | 回退路径 |
|------|----------|
| PaddleOCR | `weights/paddlex` |
| 嵌入模型 | `weights/embeddings` |

## 核心函数

```python
from server.config import get_models_config

cfg = get_models_config()
# cfg["paddleocr_dir"]   -> "<data_drive>:\\ai_models\\paddleocr" 或 "weights/paddlex"
# cfg["embeddings_dir"]  -> "<data_drive>:\\ai_models\\embeddings" 或 "weights/embeddings"
```

## 调用关系

```
config.toml [models].external_dir
    │
    ├─► get_models_config()  (server/config.py)
    │       │
    │       ├─► server/ocr.py        → paddleocr_dir
    │       └─► server/memory/config.py → embeddings_dir
    │               │
    │               └─► MemoryConfig.embedding_cache_dir
    │                       │
    │                       └─► EmbeddingEngine(cache_dir=...)  (server/memory/embeddings.py)
    │
    └─► 环境变量
            ├─► HF_HOME = <data_drive>:\ai_models\huggingface (第三方 HF SDK 缓存)
            ├─► MODELSCOPE_CACHE = <data_drive>:\ai_models\modelscope
            └─► PADDLE_PDX_CACHE_HOME = paddleocr_dir (server/ocr.py 的 _ensure_env() 设置)
```

> **坑（paddlex ≥3.7）**：`paddlex/utils/cache.py` 的 `CACHE_DIR = os.environ.get("PADDLE_PDX_CACHE_HOME", ~/.paddlex)`，
> 是**模块 import 时固化**的模块级常量，不是 `PADDLEX_HOME`（旧版变量，3.7 起已不生效）。
> 只在 `get_models_config()` 里返回正确路径、不在 paddlex import 前设置 `PADDLE_PDX_CACHE_HOME`，
> 模型会静默下载到 `~/.paddlex/official_models`，配置形同失效。详见 CHANGELOG Unreleased。

> **坑（modelscope 缓存根不对称 → 同一 repo 静默存两份）**：`modelscope/hub/utils/utils.py` 的
> `get_cache_dir()` 是 `base_path = os.getenv('MODELSCOPE_CACHE', os.path.join(~/.cache/modelscope, 'hub'))`
> 再 `join(base_path, model_id)`。注意**默认值自带 `hub`、设了环境变量就没有 `hub` 这层**：
> 不设 → `~/.cache/modelscope/hub/<org>/<repo>/`；设了 → `<MODELSCOPE_CACHE>/<org>/<repo>/`。
> SDK 会尝试自动搬家（`modelscope/hub/file_download.py` 的 `move_legacy_cache_to_standard_dir()`），
> 但条件是「legacy 存在**且**目标不存在」——**两边都在时它什么都不做**，于是
> `<cache>\hub\models\<org>\<repo>` 与 `<cache>\models\<org>\<repo>` 各留一份，旧那份从此不被解析、只占盘。
> 本项目把 `MODELSCOPE_CACHE` 指到 `<data_drive>:\ai_models\modelscope`，正是走"没有 hub 层"的那条分支。

> **坑（HF 缓存里"目录名在"不等于"模型在"）**：`HF_HOME\hub\models--<org>--<name>\` 可能只剩
> `refs\main`（下载中断留下的引用壳，整个目录约 1KB），也可能权重是
> `blobs\<sha>.*.incomplete` 而 `snapshots\<hash>\` 里只有 README——后者缺 `config.json`，
> 用 `local_files_only=True` 加载必然抛异常，看起来却"模型已就位"。
> 判断是否重复、是否可用，要看 `blobs\` 与 `snapshots\<hash>\` 的实存文件，**不能只看目录名是否存在**。

## 缓存目录里"哪一份是活的"怎么判

同一模型在多个缓存根下并存时（`embeddings/` vs `huggingface/` vs `modelscope/`），按下面顺序判，
不要凭目录名或 `du` 的大小下结论：

1. **先定解析规则**：查消费方实际用的 SDK 拼路径函数（上面两条坑就是解析规则的来源），
   确定它这次跑起来会读哪一份。
2. **比对元数据写入时间**：`.mdl` / `.msc` / `.mv`（modelscope）、`refs/main` + `blobs/` mtime（HF）。
   停更的那份通常是历史布局。
3. **列消费方引用的相对路径清单，逐条比对实存**：只有某一份独有的子目录，
   若消费方代码里 0 引用（去它的模型路径常量/枚举里 grep），就是旧版本遗留而非必需分片。
4. **确认没有第三条路径**：环境变量是否设了、有没有本地配置文件覆盖
   （如 MinerU 的 `mineru.json` → `get_local_models_dir()` 会绕过缓存根）。
   同一 SDK 有多个安装时，要逐个 venv 看版本，别假设只有一个。

> 第 3 步最容易漏：一份"看起来更大更全"的缓存，多出来的部分往往是**上一代消费方的依赖**，
> 当代版本已经不引用了；反过来，"两份内容一样、留哪份都行"的判断也要走这一步才成立。

## 新增模型引用时的规范

1. **禁止硬编码绝对路径**（如 `r"<data_drive>:\ai_models\xxx"`）
2. 在 `get_models_config()` 中添加新的子目录映射
3. 在调用处用 `get_models_config()["xxx_dir"]` 获取路径
4. 更新本文档的表格
5. 更新 `config.example.toml` 的注释

## 环境变量与配置的关系

`HF_HOME`、`MODELSCOPE_CACHE` 用于第三方库的缓存重定向，与 `get_models_config()` 是互补关系。
⚠️ **`start.bat` 并不设置这两个变量**（实测它只 `set PYTHONIOENCODING=utf-8`，见 `start.bat:37`）——它们需要由用户在系统/用户环境变量里指定，本项目只约定把缓存落到 `weights/huggingface/`、`weights/modelscope/`：

- **环境变量**：控制第三方库（HuggingFace/ModelScope SDK）的下载缓存位置
- **`get_models_config()`**：控制本项目代码加载模型的路径

两者指向同一目录（`<data_drive>:\ai_models`），确保模型只存一份。
