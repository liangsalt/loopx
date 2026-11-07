---
alwaysApply: true
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->
芯片 DV / spec.json → 测试计划 → UVM → 仿真任务使用 loopx-dv Skill 和 MCP。
LoopX 持久状态是进度依据；主 Agent 只读取 dv_status / dv_next 摘要。
将每个有界任务交给 loopx-dv-worker 子智能体；让 worker 自己 claim 和读取 context。
不要在主对话读取完整 spec、全部分块或完整日志。会话切换后先读状态恢复，不依赖聊天历史。
