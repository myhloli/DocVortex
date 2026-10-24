# B7A 验收报告：Cargo 依赖缓存与 CI 构建计时

日期：2026-10-24。实施范围沿用 [分批优化计划](optimization-plan-2026-10-23.md)。当前代码位于独立验证分支 `codex/ci-cargo-cache`，对应 [草稿 PR #38](https://github.com/myhloli/DocVortex/pull/38)。B5 仍待处理。

> 状态：缓存机制、构建来源与 CI 正确性验证完成；同一提交三轮 CI 均为 17/17 通过，最后一轮 15/15 精确命中。整体 CI 提速未证实，不将各 hosted runner 的时延波动记为性能优化成功。

## 1. 改动与边界

本批代码只涉及四个文件：

- `.github/workflows/ci.yml`：给原有构建任务接入缓存与计时，原始测试和验证命令继续执行。
- `.github/actions/cargo-cache/action.yml`：固定缓存版本、绑定 PyO3 解释器、处理未命中与恢复异常。
- `tools/ci_build_metrics.py`：记录命令耗时、Cargo 事件、实际安装的扩展来源及 wheel 摘要。
- `tests/test_ci_build_metrics.py`：20 项计时、退出码、缓存恢复、二进制来源和 Windows 编码回归。

原有 8 个 job 定义、17 个展开后的任务保持不变：三系统 × Python 3.11/3.14，中间 Python 3.12/3.13，Pydantic 下界，三平台产物与字体/历史几何比较，Windows ARM64 的两个 Python ABI 端点，以及 Rust/lint 检查。13 个源码安装任务、ARM64 wheel 构建和 Rust 检查共 15 个任务接入 Cargo 缓存。没有复用其他 job 的产品 wheel，没有削减安装、ABI 或输出检查。

本次远端验证基于 `b2ef8d02`，只提交 B7A 的四个文件。当前主工作区中的 B0–B4、B6A、B6B 改动保持原样；组合后的验收仍属于 B8。

## 2. 缓存身份与恢复

采用固定提交 `d1527c8f67ef9af357290475d4af4b0fab8f12e0`，对应 [Rust Cache v2.9.3](https://github.com/Swatinem/rust-cache/tree/v2.9.3)。命名空间为 `docvortex-ci-v2`。

缓存键包括 job、OS/架构、Rust release/host/commit hash、Cargo.lock 与 crate 清单、Cargo/Rust/编译器环境；额外区分 release ABI3/Python 版本与 dev/test，并加入根 Cargo.toml、setup.py、pyproject.toml 摘要。PyO3 使用实际选中的稳定解释器路径及完整 Python ABI 签名，相关环境也参与缓存键。Windows ARM64 的目标 triple 提前到 job 环境，使恢复与构建看到相同目标。

缓存仅保留依赖：`cache-workspace-crates=false`、`cache-bin=false`、`cache-on-failure=false`。仓库自己的三个 crate 继续从当前源码构建。该 Action 在 CI 中设置 `CARGO_INCREMENTAL=0`；产品 release 配置、Thin LTO、codegen-units 和 ABI3 下界不变。

非精确命中统一清理 target、Cargo registry/git 后构建，包括前缀回退和恢复异常。这样不会把残留的部分恢复结果当成已验证命中；工具链 bin、Cargo 配置和源码不删除。仓库变量 `DOCVORTEX_DISABLE_CARGO_CACHE=1` 可跳过恢复，在新 runner 上直接构建。

实际命令不以命中状态作跳过条件。显式 `uv pip install .` 仍要求本地源码重建；计时工具另检查当前 crate 的 Cargo 事件，并核对已安装扩展与本次 build 目录的 SHA256，拒绝从 checkout 导入旧扩展。该本地路径重建行为见 [uv 缓存说明](https://docs.astral.sh/uv/concepts/cache/)。

## 3. 测量口径

各 job 上传 `build/ci-metrics/`，保存原始日志和 JSON：Git 提交、Python、目标 triple、实际 rustc/cargo、构建输入摘要、缓存状态、命令退出码、完整命令、耗时、Cargo 编译/Fresh 事件、扩展或 wheel 摘要以及安装依赖版本。

命令时间包含依赖处理、打包等工作；Cargo 的 `Finished ... in ...` 是 Cargo 自报阶段耗时，不等于纯编译 CPU 时间。两者不能简单相减来声称得到准确下载时间。缓存恢复/保存、runner 配置、测试和排队另外通过 Actions job/step 时间记录。

冷与热运行是独立 hosted runner 上的观察，不是同机统计基准。所有轮次保留原始数据；不同任务的时延不能简单相加当作工作流墙钟收益。

## 4. 本机检查

- actionlint、Ruff 和矩阵/原命令对照通过；45 项聚焦测试通过。
- 独立检出安装后 170 项合同测试通过；原工作区的扩展和 Python 环境未被替换。
- 冷构建 Cargo 25.17 秒、命令 26.50 秒；复用依赖后 Cargo 25.06 秒、命令 26.35 秒。19 个依赖 crate 变为 Fresh，三个工作区 crate 仍编译。本机没有显著总耗时收益。
- 在隔离检出临时把原生协议哨兵 36 改为 37，已安装二进制确实返回 37，加载守卫拒绝了不匹配协议；恢复原源码并重建后回到 36。临时改动未提交。
- 模拟不完整缓存，清理指定的 target/registry/git 后完成实际干净构建和安装摘要核验。

初次本机记录受宿主 `RUST_LOG=warn` 影响，没有收到 Cargo 日志，守卫按预期拒绝把零退出码当作构建证据。原始记录保留为 `local-cold-muted*`；正式本机对照只在子进程移除此日志过滤设置，未改动用户全局环境。

## 5. 远端记录

历史基线为 [39822914298](https://github.com/myhloli/DocVortex/actions/runs/39822914298) 和 [39813565295](https://github.com/myhloli/DocVortex/actions/runs/39813565295)，均成功，任务执行跨度分别 502 秒、470 秒。原源码安装约 36–105 秒，混合了下载、构建和安装；Windows ARM64 日志中的 Cargo 构建为 79 秒。这些运行未配置 Cargo 恢复。

初始验证提交 `63489fbb` 的 [运行 40019286706](https://github.com/myhloli/DocVortex/actions/runs/40019286706) 暴露 Windows 默认代码页问题：产物身份检查读取含中文注释的源码时未指定 UTF-8，原生构建已经成功。修复提交 `1de88675` 显式按 UTF-8 读取并加入 cp1252 回归，同时更换缓存命名空间，重新建立完整冷缓存记录。失败与取消的日志均保留，不计入通过轮次。

| 同一提交 `1de88675` | 功能矩阵 | 精确缓存命中 | 任务执行跨度 |
| --- | --- | ---: | ---: |
| [第 1 次：冷缓存](https://github.com/myhloli/DocVortex/actions/runs/40020025775/attempts/1) | 17/17 通过 | 0/15，全部清理后构建 | 534 秒 |
| [第 2 次：缓存复跑](https://github.com/myhloli/DocVortex/actions/runs/40020025775/attempts/2) | 17/17 通过 | 13/15 | 1095 秒 |
| [第 3 次：波动复测](https://github.com/myhloli/DocVortex/actions/runs/40020025775/attempts/3) | 17/17 通过 | 15/15 | 691 秒 |

第二轮两个 macOS 未命中已定位：runner 预装的额外 Rust 工具链在 1.99.0 与 1.99.1 之间变化。Rust Cache 把所有已安装 Rust 工具链纳入环境键，因此缓存键变化并走了干净构建；实际使用的 1.98.1、目标与安装依赖版本均未改变。保持上游的保守失效规则，没有为了显示命中而放宽缓存隔离。

三轮在每个对应任务中的提交、实际 Python/rustc/cargo、目标 triple、构建输入、显式编译环境及已安装依赖版本一致。最终 14 个产品构建任务中，19–20 个依赖 crate 为 Fresh，仓库的三个 crate 均重新编译；Rust 检查继续执行 clippy 和 core tests。13 个安装任务的扩展均匹配本轮 build 产物，ARM64 每轮继续在两个 Python 端点加载实际 wheel。跨 runner 的二进制摘要存在差异，不把它们计为逐字节相同产物。

以下为构建/安装命令秒数；`rust-checks` 行为 clippy 命令。保留变慢的场景，不只展示收益：

| 任务 | 冷缓存 | 第一次复跑 | 波动复测 |
| --- | ---: | ---: | ---: |
| platform-artifacts-macos-latest | 100.33 | 70.57 | 125.74 |
| platform-artifacts-ubuntu-latest | 38.54 | 36.72 | 44.91 |
| platform-artifacts-windows-latest | 91.85 | 61.09 | 85.63 |
| pydantic-minimum-3.11 | 48.41 | 44.29 | 44.74 |
| pydantic-minimum-3.14 | 34.71 | 47.43 | 43.05 |
| python-minors-3.12 | 38.39 | 47.84 | 45.42 |
| python-minors-3.13 | 47.28 | 44.24 | 45.68 |
| rust-checks | 10.49 | 4.16 | 2.63 |
| test-macos-latest-3.11 | 74.64 | 73.51 | 66.66 |
| test-macos-latest-3.14 | 84.51 | 100.54 | 105.62 |
| test-ubuntu-latest-3.11 | 35.10 | 35.13 | 45.04 |
| test-ubuntu-latest-3.14 | 49.10 | 46.08 | 42.88 |
| test-windows-latest-3.11 | 95.37 | 64.37 | 92.30 |
| test-windows-latest-3.14 | 93.62 | 72.14 | 67.85 |
| windows-arm64-native-abi | 84.46 | 79.20 | 81.80 |

可确认的结构性收益是消除依赖 crate 的重复编译。Windows 3.14 安装和 clippy 在两次复跑中都更快；Linux 3.12、Pydantic 3.14 等场景仍慢于单次冷样本，不能宣称全部平台提速。Mac 3.14 最后一次 Cargo 阶段为 43.46 秒，但 uv 报告准备 58 个包用了 1 分 43 秒，整个安装为 105.62 秒；不能用 Cargo 的下降推断安装全程下降。

Mac/Python 3.11 的测试耗时为 **313.6→670.6→287.0 秒**；该任务原生扩展摘要和依赖不变，第二轮长尾未在复测中持续。三轮最长单 job 为 513、794、484 秒；任务执行跨度为 534、1095、691 秒。这里的跨度是首个 job 开始至最后一个结束，包含后续错峰启动与排队；第三轮最后的 macOS runner 就晚约五分钟开始。

因此，本批完成的是缓存正确性、兼容性覆盖和成本观测的验收，**没有建立整体 CI 提速结论，也不把仍然变慢的短样本记为性能通过**。上述三轮是 hosted runner 的观察值，不满足同机重复测量的统计条件。B7B 可据此评估进一步复用同平台 wheel；目前仍反复编译三个工作区 crate，依赖准备与全量测试也占有明显成本。

## 6. 证据与交付

证据目录：`/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B07A`。包含基线 job 数据、原始日志、各轮 artifact ZIP 与官方摘要、提取后的指标、源码失效/恢复验证、矩阵对照和全部测试日志。所有轮次下载后都核验 artifact SHA256；冷缓存产物在重跑前已单独保存。

代码提交为 `63489fbbcbbdd34bee58090ddb8b810f490bda98` 和修复提交 `1de88675b2e251774905759052d9287a8a0880b6`；三轮通过记录均针对后者。原有工作区文件和扩展摘要已核对，B0–B6B 改动未被覆盖。新增函数均有中文说明，Ruff、actionlint 和差异检查通过。

代码已推送到独立验证分支，PR 保持草稿，**未合并、未发布**。主工作区中的本批副本与报告尚未提交；B5、B7B 和 B8 仍待后续推进。
