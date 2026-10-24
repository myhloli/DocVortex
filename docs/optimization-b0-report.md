# B0 验收报告：基准门禁、独立 RSS 与冻结基线

> 完成日期：2026-10-24（Asia/Shanghai）  
> 状态：B0 已验收；B1 及之后批次尚未开始。  
> 对应计划：[分批优化实施计划](optimization-plan-2026-10-23.md)

## 1. 本批结果

已修复配对基准缺少跨版本完整输出校验、独立 RSS 汇总仍依赖旧 Python 标签的问题；补齐恢复身份、实际产物与采样失败检查，并新增文件导出专用基准。

本批只修改基准工具、针对性测试和文档。`src/`、Rust 生产源码、版本及产品依赖配置没有改动。未实施 B1–B7 的生产优化，因此本报告不宣称提速或内存改善。

- 聚焦测试：**115 passed**，21.55 秒；包括既有基准入口和四组新增回归。
- Ruff：本批 9 个 Python 文件检查通过；格式检查及 `git diff --check` 通过。
- 真实 PDF：五轮 AB/BA 配对、公开 parse、shared 文本/表格接口及两类独立 RSS 复核通过。
- 真实导出：3 个规模 × 3 个入口 × 3 种写出模式，共 **27 组**，参考与对照输出全部等价。
- 恢复运行：复用 **10 份**已有 worker 记录，文件摘要与 mtime 均未改写。
- 基线：冻结 **34 份 PDF、338 页**的输入清单及源文件；其中实际 smoke 覆盖见第 4 节，尚未执行整个集合的完整性能回放。
- 工作区已有四个 recheck 未跟踪文件的 SHA256 均保持不变。

## 2. 修改内容

| 位置 | 最终行为 |
| --- | --- |
| `tools/benchmark_native_pair.py` | 各版本内部稳定性与跨版本等价性同时检查；冻结输入集合、字节、源码资源、扩展和环境；保存完整输出及失败日志，全部通过后才输出性能比例 |
| `tests/benchmarks/rust_pdf.py` | 统一 `public_output()`，完整比较 model/middle/assets/diagnostics；源码摘要覆盖资源；记录解释器与依赖环境；非确定输出保留参考和失败轮产物 |
| `tests/benchmarks/pdf_memory.py` | 只保留原生版本比较对，统一比例与反序复测定义；恢复时核验输入、区域、源码、扩展、协议、环境和输出身份；拒绝缺失字段、空采样及无效 RSS |
| `tests/benchmarks/compare_pdf_revisions.py` | 恢复时检查 suite、次数、阶段、输入/环境身份，重读完整输出产物并重算摘要；保持 5% 阈值触发的反序复测和持续回退判定 |
| `tests/benchmarks/export_files.py` | 显式选择 `--source-root`，真实测量导出；正式时间、阶段诊断和独立 RSS 分开；控制进程流式核验基线实体文件及 RSS 种子；拒绝路径逃逸、符号链接、产物损坏和后台采样失败 |
| `tests/benchmarks/README.md` | 说明 psutil 安装、完整输出/身份规则、工具参数、恢复条件和测量边界；澄清 ReportLab 的 python/accel 选项 |

新增测试文件：

- `tests/test_benchmark_pair.py`
- `tests/test_benchmark_memory.py`
- `tests/test_benchmark_identity.py`
- `tests/test_benchmark_export.py`

新增回归覆盖：稳定但跨版本不等价、仅 middle/素材/诊断变化、单版本输出不稳定、输入或源码资源改变、扩展/协议/环境改变、空输入/缺标签、计时次数不匹配、输出文件损坏/缺失、RSS 反序复测、采样线程失败、导出种子变化、路径逃逸和输入污染。新增函数和方法均有中文文档字符串。

## 3. 冻结身份与证据位置

证据根目录：

`/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B00`

项目卷在开始时仅余约 76 MiB，因此源码快照、构建和测试产物保存在 Documents 所在卷。目录中的日期沿用计划创建日；本批在 2026-10-24 完成。

| 项目 | 值 |
| --- | --- |
| 共同生产基线 | `b2ef8d025aed2b1041da6ec6e766e9d317f716ce` |
| 参考源码 | `B00/reference-source`，由上述提交归档并独立构建 release 扩展 |
| A/A 对照源码 | `B00/control-source`，相同生产源码及该次构建的扩展副本 |
| 完整源码/资源 SHA256 | `802293be092ca7e36f2d63ef84df381933e26307b67f7d904b1eb24f9c7afc87` |
| 冻结扩展协议 | `36` |
| 冻结扩展 SHA256 | `b75a91e149cc1c7d6101ee7a186b3aa1213499403a5a9a53ef7a5edb976c4c9f` |
| 基准驱动清单 SHA256 | `9008a9b0495f840a766f36087cf4430408ba4e0ed88d6944c94ea5f2495a325e` |
| Python | Magic-PDF `.venv4`，CPython 3.14.4 |
| 关键依赖 | pypdfium2 5.13.0、NumPy 2.5.3、Pydantic 2.13.5、psutil 7.2.2；完整版本在 `identity.json` |

`identity.json` 同时记录参考、对照和当前工作树身份；三者生产源码摘要相同。参考扩展有独立构建日志，工作树原有扩展未替换。

主要证据：

| 相对证据根目录的位置 | 内容 |
| --- | --- |
| `initial-state.json` | 初始提交、工作区状态、受保护文件摘要 |
| `identity.json` | 最终源码、扩展、环境身份及受保护文件验证 |
| `corpus.json`、`inputs/` | 原始路径、冻结路径、字节摘要、页数及 34 份输入副本 |
| `reference-build.log` | 冻结参考源码的 release 原生扩展构建记录 |
| `driver-manifest.json`、`driver-snapshot/` | 18 份基准驱动、说明与回归文件的冻结归档及摘要 |
| `final-focused-tests.log`、`ruff.log` | 最终测试和静态检查结果 |
| `pdf-smoke-commands.json`、`shared-smoke-commands.json`、`export-smoke-commands.json` | 实际命令参数 |
| `resume-check.json` | 续跑前后 10 份 worker 记录的摘要与 mtime 对照 |

`driver-snapshot` 是源文件归档，不是独立安装包；恢复时将相同路径放回具备常规 fixtures/configuration 的仓库，核对 `driver-manifest.json` 后执行。基准有本地修改，不能只用 `git HEAD` 代替驱动清单摘要。

## 4. 实际验收记录

本节均为相同生产代码的 A/A 控制验证，用于证明工具能比较真实输入、保存完整产物并执行门禁；比率不作为优化收益。

| 检查 | 输入 / 配置 | 结果与证据 |
| --- | --- | --- |
| AB/BA 配对 | `2407.00079v4_origi-10.pdf`，1 页，5 轮 | 10 个版本运行全部等价；`pair-smoke/report.json` |
| public parse | `nougat.pdf`，17 页，每版本 5 次 | 首次 parse 比率 1.05565，实际触发反序复测；复测比率 1.00030、完整输出等价，未形成持续回退；`public-smoke/report.json` |
| public 独立 RSS | 同一 `nougat.pdf` 和配对计时身份 | 原生两标签汇总通过；RSS 比率约 1.00375；`memory-audit/report.json` |
| shared | `demo1.pdf`，13 页；同一冻结区域；每版本 5 次 | 文字、表格及完整共享输出等价，无持续回退；`flash-regions/`、`shared-smoke/report.json` |
| shared 独立 RSS | 同一 `demo1.pdf` 和区域身份 | 复核通过；RSS 比率约 1.00451；`shared-memory-audit/report.json` |
| 文件导出 | 素材数 8/64/256，每份 4,096 字节，5 次；write_artifact/save_bundle/export_middle_json × new/same/overwrite | 27 组全部输出等价、输入不变、独立 RSS 重放通过；`export-reference/`、`export-control/` |
| 恢复运行 | 原 public compare 与独立 RSS 命令再次执行 | 10 份已有 worker 记录完整复用，未重新写入；`resume-check.json` |

公开解析输出摘要：

`7e9e90a7d76838355ecc264b2209caedaf3083958b36d96c901bc90d155e82b8`

shared 输出摘要：

`bffcdbd2c9e55400d067c9bc9b2dcebc583e0bf5b432979328df51ae2f146a51`

配对入口的 RSS 已改为“每轮各文档独立重放峰值的最大值，再对各轮取中位数”，与旧版整份语料同进程峰值不直接比较。导出 RSS 包含准备和预热留下的驻留内存，50 ms 采样与端点观测可能漏掉更短峰值；主文件和全部旁文件的验证在控制进程或采样结束后执行。

## 5. 后续使用与范围

1. 下一批为 **B1：导出路径检查**。用本批冻结的 `reference-source` 作为共同基线，新候选使用独立源码身份和新输出目录。
2. B1/B5 使用 `export_files.py --source-root ...` 测实际文件写出；不能以 `pipeline.py` 的 render 时间代替。
3. B2/B3 继续使用冻结的原始输入，B3 显式加入两份 Form 样本。当前 34 份清单是库存冻结，完整组合回放按后续批次和 B8 执行。
4. 旧基准记录缺少 B0 新增身份字段时会被拒绝。保留旧目录，在新目录生成新报告，不手改摘要。
5. 本轮完成本机基准工具验收；没有执行全量生产 pytest、跨平台 CI、独立 wheel 安装或 MinerU 实际集成，这些不是本批完成状态的替代指标。

交付状态：本地修改和证据已保存，**未提交、未推送、未合并、未发布**。四个既有 recheck 文件保持独立边界。
