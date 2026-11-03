"""ask_user 的 outcome 档位（Qt-free，runtime 与桌面端共用）。

AskUserTool 按这些档位映射 ToolResult；把"没回答"和"拒绝回答"混成一条字符串，
agent 就会把超时读成反对。桌面侧的 Qt 桥从这里 re-export，保持旧 import 路径可用。
"""

OUTCOME_ANSWERED = "answered"     # 用户点了提交并给出选择
OUTCOME_SKIPPED = "skipped"       # 用户点「跳过，你自己定」——明确把决定权交回 agent
OUTCOME_TIMEOUT = "timeout"       # 配置的等待时间内没人回答（不是拒绝）
OUTCOME_CANCELLED = "cancelled"   # 会话被中断 / 切走了
OUTCOME_NO_HOST = "no_host"       # 没有可渲染卡片的地方（GUI 未就绪）→ 维持 fail-closed
