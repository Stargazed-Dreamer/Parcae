"""版本号唯一真源。

## 为什么单独拆一个模块

`server/main.py` 是应用入口，顶层有大量副作用（建 `app`、挂路由、打路由标签、
注册 MCP 网关、挂静态目录）。以 `python -m server.main` 启动时，该模块的名字是
`__main__`，`sys.modules` 里**没有** `server.main` 这一项。

于是任何 `from server.main import VERSION` 都会让 Python 重新 import 一遍
`server.main`，把上面那些副作用**整个再执行一次**：日志里能看到 route_tags
全量再打一遍、MCP 工具数从 120 变 123、多建一个没人用的 FastAPI app。

把常量挪出来之后，需要版本号的模块 import 这个无副作用的模块即可。

⛔ 不要为了让 VERSION「就近」而把它塞回 main.py。
"""

VERSION = "0.56.0"
