# 芯片验证能力与 TRAE 中国版

此能力将大型规范、测试计划、UVM 生成任务和仿真证据放入可恢复的项目状态。TRAE 中国版提供模型和独立子智能体上下文，LoopX 负责持久任务、产物校验、输入版本和执行证据。它是 LoopX 的增量能力；不需要另建仓库，也不需要用户手动编辑任务状态 JSON。

状态阶段、LoopX 权责边界和 UVM 组件划分见 [状态与 UVM 方法](docs/state-and-uvm.md)。当前公开入口覆盖规范分析、测试计划、UVM 生成与 build/smoke；通用 regression/覆盖率闭环尚未公开，不应将生成或 smoke 成功报告为覆盖率验收完成。

## 安装到芯片项目

准备 Python 3.11+ 与 Node.js 22.22.3+；在运行 LoopX 的环境安装可选能力依赖：

```powershell
python -m pip install "loopx[chip-verification]"
```

测试当前仓库版本时，在仓库根目录使用 `python -m pip install ".[chip-verification]"`；开发环境也可用 `uv sync --extra test`。然后在相同环境运行：

```powershell
python -m loopx.cli chip-verification --workspace D:\chips\customer-project install-trae
python -m loopx.cli chip-verification --workspace D:\chips\customer-project install-trae --execute
```

第一次只预览，第二次安装。也可直接使用模块入口：

```powershell
python -m loopx.capabilities.chip_verification.integration --workspace D:\chips\customer-project --execute
```

安装器合并 `.trae/mcp.json` 并生成 `.trae/skills/loopx-dv/SKILL.md`、`.trae/agents/loopx-dv-worker.md`、短入口规则和 `/loopx-dv` 命令。已有其他 MCP、规则与 Skill 保持原样；同名用户配置或修改过的受管理文件会得到明确冲突信息，安装器不会覆盖它们。安装目录中的 sidecar 仅记录文件归属，不承担 DV 状态管理。

在 TRAE 中国版打开该芯片项目，执行以下配置：

1. 设置 → MCP：启用项目级 MCP，确认 `loopx-dv` 服务和工具已加载。
2. 设置 → Beta → Subagents：确认已启用 Subagents 目录。
3. 使用内置 **Agent**，开启新对话，输入：`用 loopx-dv 验证 spec.json，RTL 在 rtl/，采用 UVM；按有界任务调用 loopx-dv-worker，并根据实际仿真证据报告覆盖率。`

官方确认了项目 MCP、按需 Skill、文件化子智能体的目录与加载机制。子智能体拥有独立上下文，只有内置 Agent 能调用。[MCP 配置](https://docs.trae.cn/ide_add-mcp-servers)、[Skill](https://docs.trae.cn/ide_skills)、[Subagent](https://docs.trae.cn/ide_subagents)、[规则](https://docs.trae.cn/ide_rules)

安装结果 `host_verified: false` 是有意保留的边界：文件安装和协议测试不能替代用户设备上的 TRAE 加载、委派与模型行为实测。首次使用请确认子智能体调用卡片及任务提交回执。

## 从大规范到验证证据

主 Agent 以文件路径调用 `dv_import`。规范由服务本地解析，模型不接收整份文件。`dv_next` 返回下一项工作摘要；主 Agent 委派 worker，worker 用 `dv_claim` 取得 attempt，再用 `dv_context` 取得有限输入和响应 schema。默认上下文预算为 24,000 字符，包括序列化后的输入、schema 与指令；字符预算不是模型 token 数的保证。

worker 根据契约生成响应并 `dv_submit`。服务校验通过才推进任务。耗时工作可 `dv_heartbeat` 续约，失败用 `dv_abandon` 写明原因。重新打开对话或重启 MCP 后从 `dv_status` 恢复；一次 worker 调用只处理一个任务，避免把全部分块累积到一个上下文。

`dv_design_map` 查看系统、微架构、模块与功能关系；`dv_generation_scopes` 查找可生成的 UVM 范围。`dv_generate_uvm` 根据 RTL、顶层和 profile 准备任务。它可能直接返回已领取的 attempt，此时 worker 使用该 attempt 读取上下文，无需再次 claim。生成内容需满足 transaction、sequence、driver、monitor、scoreboard、coverage、env、test 与顶层的契约一致性。

`dv_build_uvm` 提交后台 EDA 作业，默认使用 Vivado 并执行 smoke；通过 `dv_job_status` 读取进度和诊断。长时间仿真不阻塞一次 MCP 调用。`dv_report` 报告真实 build/smoke 证据的校验结果与限制；通用覆盖率目前未知，不能用任务完成率充当覆盖率。

## 会话与能力边界

TRAE 的主会话只保留目标、任务摘要与产物引用。完整上下文只进入 worker。`session_id` 是调用方提供的执行标签，`attempt_id` 是服务持久化的提交凭据；它们不声称是经 TRAE API 认证的原生会话身份。协议可验证恢复和过期提交拒绝，但每次重复委派是否创建全新宿主子会话仍需从实际 TRAE 调用记录确认。

可以在内置 Agent 使用原生 `/goal` 继续推进长任务；本适配不调用外部会话派发 API，不创建隐藏 heartbeat。它专门面向 TRAE 中国版 IDE，与 LoopX 已有 TraeX 入口分开。官方 CLI 2.0 支持 `traecli exec`/ACP，但限定企业旗舰套餐、需要单独登录；普通 IDE 登录并不自动等于 CLI/API 授权。[Goal 工作流](https://docs.trae.cn/ide_built-in-workflows)、[CLI 使用限制](https://docs.trae.cn/cli_about-trae-code-cli-2)

## 验证与排错

`dv_doctor` 检查 provider 和 EDA 环境。MCP 启动失败时检查安装时的 Python 环境是否仍然存在、是否安装 MCP，以及工作区路径。安装器将执行程序与参数分开；带空格的解释器目录通过显式 PATH 解析，工作区通过 `${workspaceFolder}` 传入。MCP 继承安装时写入的 PATH；先用 `node --version` 确认当前解析到的是 Node.js 22.22.3+，再安装。系统存在多个 Node 时将合格版本的目录放在当前终端 PATH 前面即可，不必修改机器全局配置。依赖环境迁移后重新运行安装；若用户修改过配置，先按冲突提示处理。

自动测试覆盖真实 stdio 的初始化与工具调用、跨连接持久状态、无效参数拒绝，以及安装预览、重复安装、保留用户配置和失败回滚。它们验证本地接入层，不能证明任意芯片设计已达到某一覆盖率，也不能替代 TRAE 实机行为检查。
