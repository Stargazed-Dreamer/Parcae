"""T12: file_edit 工具（精确字符串替换）"""

from __future__ import annotations

from lib.agent_runtime.builtin_tools._write_backup import backup_before_overwrite
from lib.agent_runtime.builtin_tools.base import BuiltinTool, is_path_safe
from lib.agent_runtime.types import ToolCall, ToolResult


class FileEditTool(BuiltinTool):
    """精确字符串替换编辑文件。

    T19：改文件同样先留可回滚副本（safety 档写的是"可逆"，就得真能退回去）。
    """

    operation_id = "file_edit"

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        args = tool_call.args or {}
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))

        if not old_string:
            return self._error(tool_call, "old_string is required", "invalid_argument")

        safe, reason = is_path_safe(file_path)
        if not safe:
            return self._error(tool_call, f"Path not safe: {reason}", "path_forbidden")

        try:
            from pathlib import Path
            p = Path(file_path)
            if not p.exists():
                return self._error(
                    tool_call, f"File not found: {file_path}", "not_found"
                )
            content = p.read_text(encoding="utf-8")

            count = content.count(old_string)
            if count == 0:
                return self._error(
                    tool_call,
                    f"old_string not found in {file_path}",
                    "string_not_found",
                )
            if count > 1 and not replace_all:
                return self._error(
                    tool_call,
                    f"old_string appears {count} times in {file_path}. "
                    f"Provide a longer unique context or set replace_all=true.",
                    "string_not_unique",
                )

            if replace_all:
                new_content = content.replace(old_string, new_string)
                replaced = count
            else:
                new_content = content.replace(old_string, new_string, 1)
                replaced = 1

            # T19：改前先留一份可回滚副本（文件必然已存在，一定会备到）
            backup_path = backup_before_overwrite(p)
            p.write_text(new_content, encoding="utf-8")
            message = f"Replaced {replaced} occurrence(s) in {file_path}"
            if backup_path:
                message += f"（改前的原文件副本：{backup_path}）"
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
                f"Edit failed: {type(e).__name__}: {e}",
                "edit_error",
            )
