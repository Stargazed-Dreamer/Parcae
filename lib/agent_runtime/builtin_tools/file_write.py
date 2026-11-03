"""T11: file_write 工具（覆盖写入，自动创建父目录）"""

from __future__ import annotations

from lib.agent_runtime.builtin_tools._write_backup import backup_before_overwrite
from lib.agent_runtime.builtin_tools.base import BuiltinTool, is_path_safe
from lib.agent_runtime.types import ToolCall, ToolResult


class FileWriteTool(BuiltinTool):
    """写文件（覆盖模式），自动创建父目录。

    T19：覆盖已存在的文件前先留一份可回滚副本——safety 注释里的"可逆"
    从今往后是真的可逆。
    """

    operation_id = "file_write"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        safe, reason = is_path_safe(file_path)
        if not safe:
            return self._error(tool_call, f"Path not safe: {reason}", "path_forbidden")

        try:
            from pathlib import Path
            p = Path(file_path)
            backup_path = backup_before_overwrite(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            byte_count = len(content.encode("utf-8"))
            message = f"Wrote {byte_count} bytes to {file_path}"
            if backup_path:
                message += f"（覆盖前的旧内容副本：{backup_path}）"
            return self._success(
                tool_call,
                message,
            )
        except PermissionError:
            return self._error(
                tool_call, f"Permission denied: {file_path}", "permission_denied"
            )
        except Exception as e:
            return self._error(
                tool_call,
                f"Write failed: {type(e).__name__}: {e}",
                "write_error",
            )
