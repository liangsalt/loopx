---
name: loopx-dv
description: 使用 LoopX 持久状态与独立子智能体推进芯片 UVM 验证
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->
使用 loopx-dv Skill 处理用户的芯片验证目标。先读取 dv_status，再按 dv_next 的摘要将一个任务交给 loopx-dv-worker。规范、产物与日志按引用保留，完整 context 仅由 worker 读取。依据 MCP 接受的产物和实际仿真证据继续推进。
