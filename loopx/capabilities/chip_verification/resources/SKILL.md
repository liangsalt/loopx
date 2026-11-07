---
name: loopx-dv
description: 从任意层级的大型芯片规格开始，用共享约定推进 RTL/IP 设计与 UVM 验证；状态独立于 Agent 会话。
---

<!-- loopx-managed-chip-verification:trae-cn-ide:v1 -->

使用项目的 loopx-dv MCP。选定 runtime 提供模型和会话，LoopX 保存规格、关系、任务、租约、产物和工具证据。设计与 DV 是控制面的两条工作线，不要求两个 Agent。用户提供目标与文件路径，无需维护状态 JSON。

1. 首先 dv_status；未导入时按路径 dv_import，默认 flow=design-dv。FS、AS、微架构和混合输入均可接入。兼容纯 DV 可选 flow=dv。文件由服务读取，不要把整份 spec 放进对话。
2. dv_next 取得摘要；当前 runtime 用 dv_claim、dv_context、dv_submit 处理一个有界任务。可选 loopx-dv-worker 隔离上下文。不要累积已完成任务的材料；接近宿主限制时换会话，从状态恢复。执行标签不是经过宿主认证的原生会话身份。
3. worker 返回提交回执后，重新读取状态。只有服务接受的产物才算该任务已完成。失败先读原因，修正输入或环境后再 dv_retry；遇到相同未解决原因停止重复尝试并报告。
   已接受的 RTL/UVM 组件若在工具检查中暴露错误，调用 dv_revise(task_id, reason)，附上具体诊断后重新领取；依赖证据会失效。工具任务按 dv_retry 返回的 check-design/build-uvm 操作重新运行，不能当作模型任务领取。不得改旧产物或回执来制造通过。
4. 共享约定固定模块、接口、关系和配置，区分来源事实与设计选择。按状态完成 contract-plan、microarchitecture、rtl；用 dv_engineering_status 读进度。已有微架构可直接承接后续任务，不补造上层 FS/AS。影响正确性的缺口显式记录。dv_generation_scopes 确认范围后，以 contract_task_id、top 调用 dv_generate_uvm，可省略 rtl_files，在 RTL 完成前生成 UVM。预期行为来自规格；不得将生成 RTL 的行为当唯一正确性标准。transaction、sequence、driver、monitor、agent、scoreboard、coverage、env、test 与顶层保持一致。
5. dv_check_design 启动 RTL 工具检查，dv_job_status 查询；工具缺失或不支持的检查不能算通过。dv_join_design 将 RTL 与 UVM 清单绑定到相同约定，再 dv_build_uvm 联合仿真。EDA 后台运行，不嵌入一次 MCP 同步等待。只取有界诊断和文件引用。
6. 用 dv_report 读取证据。明确功能覆盖率、代码覆盖率、断言结果和计划完成率的差别，报告分母、采样范围、未覆盖项与仿真来源。不能将规划进度或未执行的 covergroup 当作 90% 覆盖率。

会话只保留目标、当前任务必要材料和产物引用。重开对话或重启 MCP 后从 dv_status 恢复，不重导入相同 spec 来模拟继续。有效 attempt 可恢复，失效后重新领取；不手工修改状态库、回执或指标。

上下文阻塞会记录所需字符数和完整输入引用。UVM 集成组件可能比需求抽取需要更多上下文；确认宿主还容得下指令、工具、输入与输出后，可对同一范围重新调用 dv_generate_uvm 并明确设置 context_chars。它只调整该次任务预算，不改变规格分片、共享约定或已经接受的组件。字符数不是 token 数；不得自动取消上限、删掉必要依赖或把整个规格装回会话。

TRAE 中国版启用项目级 MCP 即可，Subagents 可选。可使用宿主原生 /goal 推进多轮任务；此 Skill 不创建外部 heartbeat，不调用 TraeX，不假设 IDE 登录可供外部 API 使用。安装和协议测试不等于宿主 UI 与模型行为已经实测。
