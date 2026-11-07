---
name: loopx-dv
description: 用 LoopX 对大型芯片 spec 建立可恢复的 DV 状态，分任务生成测试计划与 UVM 套件，运行仿真并依据真实证据分析覆盖率。
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->

使用项目的 loopx-dv MCP。TRAE 提供模型与独立 Subagent 上下文，LoopX 保存规范索引、任务、租约、产物与仿真证据。用户描述目标、提供输入路径即可，无需手工维护状态 JSON。

1. 首先调用 dv_status；未导入时按用户输入路径调用 dv_import。文件由服务读取，不要把整份 spec 放进对话。
2. 调用 dv_next 取得任务摘要。让内置 Agent 将一个任务委派给 loopx-dv-worker，并只传任务 ID、目标及输出边界。完整上下文由 worker 自己读取。一次委派处理一个任务；下一个任务重新委派，避免将所有分块累积在一个 worker 会话。以实际宿主调用记录确认隔离，不把执行标签当作原生会话证据。
3. worker 返回提交回执后，重新读取状态。只有服务接受的产物才算该任务已完成。失败先读原因，修正输入或环境后再 dv_retry；遇到相同未解决原因停止重复尝试并报告。
4. 测试计划稳定后，用 dv_generation_scopes 确认模块/功能范围，指定 RTL、顶层和适用 profile 调用 dv_generate_uvm；继续通过 worker 完成生成任务。遵循所返回的接口与 UVM 组件契约：transaction、sequence、driver、monitor、agent、scoreboard、coverage、env、test 和顶层连接保持一致。
5. 用生成清单调用 dv_build_uvm。EDA 在后台运行，使用 dv_job_status 查询回执；不要把耗时仿真嵌入一次 MCP 同步等待。只取有界诊断和文件引用。
6. 用 dv_report 读取证据。明确功能覆盖率、代码覆盖率、断言结果和计划完成率的差别，报告分母、采样范围、未覆盖项与仿真来源。不能将规划进度或未执行的 covergroup 当作 90% 覆盖率。

主会话只保留目标、当前摘要和产物引用。重新打开对话或重启 MCP 后从 dv_status 恢复；不重导入相同 spec 来模拟继续。若中断的租约仍有效，交给 worker 恢复；失效后根据服务状态重新领取，不重复提交旧 attempt。

安装后须在 TRAE 中国版启用项目级 MCP 和 Subagents，使用内置 Agent。可使用其原生 /goal 推进多轮任务；此 Skill 不创建外部 heartbeat，不调用 TraeX，不假设 IDE 登录可供外部 API 使用。
