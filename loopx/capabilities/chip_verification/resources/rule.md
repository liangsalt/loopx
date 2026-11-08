---
alwaysApply: true
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->
芯片规格 → RTL/IP 与 UVM → 仿真任务使用 loopx-dv Skill 和 MCP。
LoopX 持久状态是进度依据；先读取 dv_status / dv_next 摘要。
当前 runtime 按模块、功能、关系或工程步骤逐个 claim、读取完整任务 context、submit；可选 loopx-dv-worker 隔离上下文，不用字数定义分工。
不要读取完整 spec、累积全部分块或完整日志。切换会话后先读状态恢复。
从功能、架构或微架构均可开始；两条线使用相同模块、接口和关系版本。
