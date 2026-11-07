---
name: loopx-dv
description: 使用 LoopX 持久状态推进芯片 RTL/IP 设计与 UVM 验证
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->
使用 loopx-dv Skill 处理用户的芯片设计和验证目标。先读取 dv_status，再按 dv_next 领取一个有界任务、读取 context 并提交。当前 runtime 可以直接执行，也可选用 loopx-dv-worker；两条线无需两个 Agent。规范、产物与日志按引用保留。依据 MCP 接受的产物和真实工具证据推进。
