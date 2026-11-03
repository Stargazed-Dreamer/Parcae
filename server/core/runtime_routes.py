"""运行时端点路由：让下游知道「后端现在在哪个端口」，并可登记自己。

## 背景

后端首选端口可能被 Windows 端口保留区占用（`WinError 10013`），启动时会按序
顺延（`lib/port_binding.py`）。此时所有写死端口的消费方必须能找到新端口：

- **入向查询**：`GET /runtime/port` —— 客户端 / 未来的本地管家前端 / 外部脚本
- **出向登记**：接入方用 `POST /runtime/consumers` 自报自己的配置文件路径，
  之后端口变更时后端会自动把该文件里的旧端口改掉（`lib/runtime_endpoint.py`）

## 端点

| 方法 | 路径 | 语义 | agent 可调 |
|---|---|---|---|
| GET | `/runtime/port` | 当前端口声明（含是否顺延、原因） | 是（只读） |
| GET | `/runtime/consumers` | 已登记下游 + 各自配置里写的端口 | 是（只读） |
| POST | `/runtime/consumers` | 登记/更新一个下游消费方 | **否**（显式标 x-agent-callable=False） |

⛔ POST 会写运行时文件并影响下次端口同步，属于运维面操作，不给 agent 调用。
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from lib.runtime_endpoint import (
    consumer_ports_in_use,
    load_consumers,
    read_declaration,
    register_consumer,
)
from lib.schema import BaseSchema
from server.config import get_server_config

logger = logging.getLogger("parcae.runtime_routes")

router = APIRouter(prefix="/runtime", tags=["runtime"])


def _actual_listen_port(scope: MutableMapping[str, Any]) -> int | None:
    """从 ASGI scope 取本连接真正落到的**监听端口**。

    uvicorn 会把实际 bind 的 (host, port) 放进 `scope["server"]`，这是本进程
    「我现在到底在哪个端口」的唯一可信来源 —— 比 config.toml 可靠，因为 config
    里写的只是**首选**端口，一旦顺延就对不上。
    """
    server = scope.get("server")
    if isinstance(server, (list, tuple)) and len(server) >= 2 and isinstance(server[1], int):
        return int(server[1])
    return None


@router.get("/port", summary="当前后端实际端口（运行时声明）")
def runtime_port(request: Request) -> dict:
    """当前后端端口。

    优先返回**本进程**写的运行时声明（端口顺延时 port != preferred_port）。

    ⛔ 声明缺失或属于别的进程时**不能**回退 config.toml：config 里是首选端口，
    顺延场景下会张冠李戴（2026-10-20 实测：9808 上的实例自报 8766，因为声明被
    另一个测试实例覆盖过）。此时取 ASGI scope 里的实际监听地址；连它都拿不到
    （非 ASGI 直连的嵌入式调用）才回退 config，并在 reason 里写明不确定。
    """
    decl = read_declaration()
    if decl and decl.get("pid") == os.getpid():
        return {"source": "declaration", **decl}

    cfg = get_server_config()
    host = cfg["host"]
    scheme_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    preferred = int(cfg["port"])

    actual = _actual_listen_port(request.scope)
    if actual is None:
        return {
            "source": "config",
            "host": host,
            "port": preferred,
            "preferred_port": preferred,
            "base_url": f"http://{scheme_host}:{preferred}",
            "degraded": False,
            "reason": "无运行时声明且拿不到实际监听端口（嵌入式/TestClient 调用）",
            "pid": os.getpid(),
            "status": "unknown",
        }

    return {
        "source": "self-scope",
        "host": scheme_host,
        "port": actual,
        "preferred_port": preferred,
        "base_url": f"http://{scheme_host}:{actual}",
        "degraded": actual != preferred,
        "reason": "运行时声明缺失或属于另一个进程；端口取自本进程实际监听地址",
        "pid": os.getpid(),
        "status": "unknown",
    }


@router.get("/consumers", summary="下游消费方登记表（谁写死了我们的地址）")
def runtime_consumers() -> dict:
    """已登记的下游配置文件清单，以及它们当前写的端口（排查失联用）。"""
    items = load_consumers()
    return {"count": len(items), "consumers": items, "ports_in_use": consumer_ports_in_use()}


class ConsumerRegistration(BaseSchema):
    """下游自注册请求。"""

    id: str = Field(..., min_length=1, max_length=64, description="唯一标识，如 butler-frontend")
    path: str = Field(..., min_length=1, description="配置文件绝对路径（支持 ~ 展开）")
    kind: str = Field("mcp-json", description="配置类型，仅作标注：mcp-json / claude-json / env / other")
    auto_rewrite: bool = Field(True, description="端口变更时是否自动改写该文件里的回环 URL")
    note: str = Field("", description="备注（谁在用、为什么登记）")


@router.post(
    "/consumers",
    summary="登记下游消费方（端口变更时自动同步其配置）",
    openapi_extra={"x-agent-callable": False},
)
def register_runtime_consumer(body: ConsumerRegistration) -> dict:
    """登记/更新一个下游消费方。

    ⛔ 不校验 path 是否"合法"到能写任意文件的程度 —— 真正的安全边界在
    `lib.runtime_endpoint.rewrite_consumers()`：只有文件里出现
    `127.0.0.1:<旧端口>` 这样的回环 URL 时才会替换端口号，其余内容一律不动，
    且写前会备份 `.bak_portrewrite`。
    """
    entry = register_consumer(
        body.id,
        body.path,
        kind=body.kind,
        auto_rewrite=body.auto_rewrite,
        note=body.note,
    )
    logger.info("下游消费方已登记: %s → %s", entry["id"], entry["path"])
    return {"ok": True, "consumer": entry}
