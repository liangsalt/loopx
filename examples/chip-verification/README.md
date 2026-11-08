# 芯片设计与 UVM 验证案例

## 仓库内的复杂题目与参考答案

`cases/` 保存两个可离线使用的公开题目，以及同一 OpenTitan 固定版本的实际参考 RTL、UVM、测试计划和 FPV 文件。每题 `input/spec.json` 的**独立上游规格正文大于 512 KiB（524,288 字节）**，JSON 包装、重复文档、作者补充的系统题设、RTL、验证代码和许可证均不计入门槛。两题共享的协议资料在每题只计算一次。

| 题目 | 设计范围 | spec.json 字节 | 独立规格正文字节 | 题目与答案 |
| --- | --- | ---: | ---: | --- |
| 串行 I/O 子系统 | SPI Host、SPI Device、USB Device；TL-UL 访问、FIFO/缓冲、模式切换、错误与中断、时钟复位和外部连接约束 | 953,979 | 567,411 | [opentitan-serial-io](cases/opentitan-serial-io/README.md) |
| 常开控制子系统 | 电源、复位、时钟、系统复位、引脚复用与告警处理；低功耗进入/退出、唤醒、保持、复位原因、升级处理 | 1,152,997 | 592,589 | [opentitan-aon-control](cases/opentitan-aon-control/README.md) |

每题从作者明确标注的系统题设，连接到原始架构、微架构和模块/寄存器规格。设计线与 DV 线读取同一份 `module_graph.json` 和集成约束。这些文件是输入材料与验收依据，不是另一个任务状态管理器；运行状态仍由 LoopX 保存。

任务按输入的模块、功能、结构章节和工程步骤分解，不按字符数切片。完整行为对象、章节与必需依赖不会为了凑长度而截断；长度只作为观测信息，不是导入、领取或生成的验收门槛。无显式结构的正文保持完整，语义细化仍需模型结合来源完成。历史验证回执中的字符预算是当时版本的记录，不是当前任务边界或成功标准。

可对本页四份题目实测结构分解、逐任务完整源及依赖、最大上下文领取、跨进程恢复，以及旧长度参数不改变边界。使用待验证的 LoopX 环境运行，目标必须是仓库外的新目录；摘要只记录实际检查结果，不运行模型或 EDA、不产生覆盖率：

```powershell
python examples/chip-verification/semantic_partition_trial.py --destination ..\chip-runs\semantic-01
```

两题现已直接修订 46 个不同的问题编号（串行题适用 27 项、常开题适用 25 项，部分共有）。`input/problem.md` 给出生效规则；`input/adjudications/` 的单项规则同时嵌入 `spec.json`，以原文哈希和行范围绑定。LoopX 分片后将相关规则作为必需依赖送入上下文，要求保留原文与修订的共同引用，再传向同一设计/DV契约。明确的练习约定标注作者权限，原始上游文件保持不变。ABI、pad 能力和少量精确时序缺口只约束依赖它们的验收项，不能伪装为已解决或已覆盖；背景差异不扩大 DUT 范围。

目录中的 `answer_guide.md` 和 `obligations.json` 解释预期行为、来源、负向测试、覆盖维度与已知歧义；共享 `reference/` 保存锁定版本的上游实现。答案用于语义和接口比较，不要求候选 RTL 与上游逐字一致。Pinmux 在该版本具有 FPV/安全测试计划，缺少同版本独立 UVM 环境，不能用其他模块的 UVM 代替其验收。

先在仓库根目录离线校验，再导出一个**仓库外的新目录**作为待测宿主工作区：

```powershell
python examples/chip-verification/case_tool.py validate
python examples/chip-verification/case_tool.py stage --case opentitan-serial-io --destination ..\chip-runs\serial-io-01
python -m loopx.cli chip-verification --workspace ..\chip-runs\serial-io-01 import --spec spec.json --flow design-dv --execute
```

另一题使用 `--case opentitan-aon-control` 和独立目标目录。导出器只复制题目与许可证，不复制答案或旧运行状态，也不会覆盖已有目录。这是输入物料隔离，不能约束宿主访问其他目录或网络。公平评测还需为候选运行设置实际访问权限和评测规则。不要在包含答案的 LoopX checkout 中让候选 Agent 直接解题。

Codex、TRAE 中国版及其他宿主使用现有 [chip-verification 能力](../../loopx/capabilities/chip_verification/README.md)，通过状态查询、领取任务、限定上下文和结果提交推进两条线。只有一个 Agent runtime 也可以运行；更换会话不改变项目状态。不要将整个大规格粘贴进对话。

参考材料已锁定来源并保留许可证，但依赖闭包与本机仿真基线尚未合格。完整验收必须分别进行“候选 RTL × 参考 TB”“候选 TB × 参考 RTL”、错误变异检出，以及固定覆盖分母下的回归。`case_tool.py` 只证明文件完整性、输入隔离和大小；它不执行仿真，不生成覆盖率百分比。

本目录的静态公开题库是明确选定、带许可证的仓库内容。下面的旧版下载式 starter 保持原有行为；其临时下载、用户设计、生成物和运行日志仍留在忽略目录，不因新增题库而变为应提交材料。

离线回归：

```powershell
python -m unittest discover -s examples/chip-verification -p test_case_tool.py -v
python -m pytest -q examples/chip-verification/test_opentitan_adjudications.py
```

## 来自 Coral NPU issue 的复杂题目

[`issue_cases/`](issue_cases/README.md) 另存两份以真实故障为入口的系统集成题。题面贯穿系统观察、ISA/CSR、调度与微架构、模块边界、RTL 修复和独立 UVM 验证；候选输入保存修复前源码种子，参考修复、补丁和上游回归只在答案目录。

| 题目 | 来源 | spec.json 字节 | 独立原始上下文字节 |
| --- | --- | ---: | ---: |
| 零元素向量操作退休与前进性 | [Coral NPU #89](https://github.com/google-coral/coralnpu/issues/89) | 647,013 | 606,813 |
| FENCE.TSO 合法性与内存排序 | [Coral NPU #130](https://github.com/google-coral/coralnpu/issues/130) | 666,546 | 625,849 |

Coral 的字节列统计保留的原始上下文，包含范围策略明确排除的 ISA 背景，不能等同于全部适用规范。规格锁定 RISC-V ISA 20240411 和各题修复前的 Coral 提交，不混用旧 Kelvin SIMD 与 RVV 配置。简短的范围策略随每个原文片段提供。两题均要求集成已固定的现有 IP，而非从通用 ISA 章节重造完整 NPU。

```powershell
python examples/chip-verification/coral_issue_cases.py validate
python examples/chip-verification/coral_issue_cases.py stage coralnpu-vector-zero-retirement ..\chip-runs\coral-zero-01
python -m loopx.cli chip-verification --workspace ..\chip-runs\coral-zero-01 import --spec spec.json --flow design-dv --execute
python -m unittest discover -s examples/chip-verification -p test_coral_issue_cases.py -v
```

另一题使用 `coralnpu-fence-tso-ordering` 和新的导出目录。完整仓库、构建依赖和 EDA 仍需在实验机器准备；补丁来源校验不代表 RTL/UVM 仿真通过。90% 功能覆盖率是冻结适用覆盖分母后的执行验收目标，不是题库生成结果。

## 下载式入门资料集合

这里提供两个固定版本的公开资料集合，使用标准 Python 下载并生成可导入 LoopX 的 `spec.json`。每个案例同时包含系统、微架构和模块层资料，规范角色的独立自然语言正文超过 200 KiB；RTL、已有测试、测试计划、软件和许可证不用于凑这个大小。

| 案例 | 范围 | 固定版本 |
| --- | --- | --- |
| `opentitan_eg1` | Earlgrey 系统；电源、复位、时钟、常开定时器、引脚和跨模块唤醒约束；已有 UVM/RTL/软件参考 | [OpenTitan `c69eaea`](https://github.com/lowRISC/opentitan/tree/c69eaea24a57c581ed274779887cb4a0c1405e3b) |
| `cheshire` | Cheshire SoC；CVA6 流水线/MMU、AXI 互连、LLC、iDMA、启动与软件接口；集成 RTL/测试参考 | [Cheshire `6c6dd5e`](https://github.com/pulp-platform/cheshire/tree/6c6dd5e834347f72c7be31cd6c41021ec293c5b9)，依赖资料固定在其依赖锁对应的提交 |

这两个案例用于体验大型资料的分解、来源追踪、限定上下文、测试计划和跨会话恢复。它们不是可直接运行的完整 SoC 仿真工程，也不承诺 SoC 90% 覆盖率。生成候选 TP/UVM、资料解析完成或包大小达标均不能作为仿真覆盖率。后续仿真还需要完整 RTL/依赖、配置、可用 EDA、独立 checker 和明确覆盖率分母。

## 生成并导入

从 LoopX 仓库根目录运行，Python 3.11+ 即可获取资料，无需其他仓库或 Python 第三方依赖：

```powershell
python examples/chip-verification/fetch.py --workspace .local/chip-starters
```

脚本下载 66 份固定提交下的文件（含许可证），每份同时核对 Git blob SHA-1 和固定 SHA-256，失败时退出，不用最新分支替代。原文不会写进 LoopX 的 Git 源码目录，输出分别位于：

```text
.local/chip-starters/opentitan_eg1/spec.json
.local/chip-starters/cheshire/spec.json
```

选一个案例开始，例如：

```powershell
loopx chip-verification --workspace .local/chip-starters/opentitan_eg1 doctor
loopx chip-verification --workspace .local/chip-starters/opentitan_eg1 import --spec spec.json --execute
loopx chip-verification --workspace .local/chip-starters/opentitan_eg1 design-map
loopx chip-verification --workspace .local/chip-starters/opentitan_eg1 install-trae --execute
```

在 TRAE 中国版 IDE 打开该案例文件夹，按能力说明启用项目 MCP 和 Subagents，然后使用 `/loopx-dv`。主会话只读取状态和下一项工作，工作子智能体领取并读取一个限定大小的上下文包。每个案例使用独立工作区，不要将两个案例的状态混在一起。

## 来源、角色与重现

- `catalogs/*.json` 只保存固定提交、文件路径、两个哈希、文档层级、角色与候选关系，不包含原文。
- 输出 `sources/` 保存原始文件字节，`source_manifest.json` 保存来源 URL、哈希、字节统计、角色、层级和限制；`catalog.json` 是本次构建使用的来源清单。
- `UPSTREAM_LICENSES.md` 指向各仓库保留的 `LICENSE`。OpenTitan 声明 Apache-2.0；Cheshire 及相关 PULP 仓库保留其原始 Solderpad 许可证文件和源文件版权头。具体文件仍以原始声明为准。
- `reference` 角色不会变成规范要求。已有 RTL、UVM、测试计划、软件，以及不匹配选定核配置或仅提议中的行为，必须保留其参考属性。`specification` 文档仍需针对具体 RTL 参数与版本进行适用性审查。
- 跨层关系均标记 `Candidate; unproven.`；`verifies` 关系名也只是候选关系，不代表已执行验证。
- `fetch_receipt.json` 单独记录网络下载、现有文件或校验过的缓存来源，不影响生成规范的哈希。

只获取单个案例，以及重新校验已下载的文件：

```powershell
python examples/chip-verification/fetch.py --workspace .local/chip-starters --case cheshire
python examples/chip-verification/fetch.py --workspace .local/chip-starters --offline
```

可选的离线缓存采用相同的 `案例名/sources/仓库名__项目名/原始路径` 结构。缓存必须同时通过两个固定哈希校验：

```powershell
python examples/chip-verification/fetch.py --workspace .local/chip-starters-copy --offline --cache-root .local/chip-starters
```

未提供 `--offline` 时，只有网络失败且显式提供了 `--cache-root` 才会回退到校验过的缓存，回退会写入获取收据。已有文件哈希错误时直接拒绝，不静默覆盖。下载式 starter 生成的原文、规格、仿真日志或用户验证状态留在本地，不提交到 LoopX 源码库。

获取器的本地完整性检查不访问网络：

```powershell
python -m unittest discover -s examples/chip-verification -p test_fetch.py -v
```
