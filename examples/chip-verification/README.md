# 复杂芯片规范入门案例

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

未提供 `--offline` 时，只有网络失败且显式提供了 `--cache-root` 才会回退到校验过的缓存，回退会写入获取收据。已有文件哈希错误时直接拒绝，不静默覆盖。不要将生成的原文、规格、仿真日志或用户验证状态提交到 LoopX 源码库。

获取器的本地完整性检查不访问网络：

```powershell
python -m unittest discover -s examples/chip-verification -p test_fetch.py -v
```
