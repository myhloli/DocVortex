# R1 电子表格 Rust 优化暂停检查点

状态：**2026-10-25 按用户要求暂停**。代码和证据均已保存；R1 尚未完成最终验收，不启动后续测量、导出或安装任务。

恢复记录：用户已于2026-10-25要求继续；已核对最终源码和扩展身份并恢复验收。下文保留暂停时的检查点，当前进度以分批计划和后续 R1 验收报告为准。

完成记录：R1最终性能、RSS、导出、视觉和Python3.11/3.14安装验收已通过，见 [R1验收报告](optimization-r1-report.md)。下文的待办清单为暂停时历史状态。

## 当前位置

- 工作区：`/Users/myhloli/.codex/worktrees/nonpdf-optimization/docvortex`
- 分支：`codex/nonpdf-optimization`，HEAD 为 `1e7bc45aa7feb50bf305be46dbefe718f91bbfee`。
- 首轮 N0–N6 保留原有未提交改动；本批 R1 叠加在其上。未提交、未推送或发布。
- 证据目录：`/Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R1`。
- 解释器：`/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python`。
- 当前私有协议为 **37**，源码目录已有匹配的 release 扩展。

## 已完成

1. 增加 Rust 电子表格区域核和 PyO3 批量接口，支持三档 gap 的发现、评分、稳定选择，以及严格语义子集筛选。采用稀疏区间/矩形索引统计，不分配整个 bbox 的布尔矩阵；等长集合不作无意义的严格包含比较。
2. XLS/XLSX 和 ODS 接入。XLS/XLSX 保持全部选中区域先物化，再用物化语义筛选；DISPIMG 副作用及自定义发现/评分钩子保留原回调顺序。ODS 仅把正常整数跨度送入原生层，浮点或超 i64 跨度保留原行为。
3. 保留冻结 Python oracle，逐阶段比较区域、成员、全部评分字段及 `float.hex()`，并覆盖真实格式入口。
4. 更新协议检查、注册合同及原生调用计数，旧协议36在当前源码下明确拒绝。
5. Rust core **26项单元测试、8项集成测试**通过；`cargo fmt` 和 workspace Clippy `-D warnings` 通过。初次格式检查的缺失括号已在构建前修正。
6. 初版聚焦 **994项通过**；兼容边界追加 **13项通过**。
7. 最终代码的完整 pytest 已在暂停前结束：**11975 passed、14 skipped、6 warnings，204.86秒**。日志为 `full-pytest-02.log`。暂停检查时已无该测试进程运行。

## 冻结身份

| 用途 | 路径 | 源码摘要 | 扩展摘要 |
| --- | --- | --- | --- |
| 本批基线，N6已验收源码/协议36 | `reference-source` | `38f66624c911e424fa910db5c0a8f6b05800adfe02920037da181a00a5b97913` | `499266192fa5eb363500041d453b2485073d5a4473f9451485f3d8932ca8f703` |
| 初版R1，保留其测量证据 | `candidate-source` | `24462288da9d3a5bab49d4418ac7b5692cd93b102aeb3d868fc22fabc3f314db` | `2dd40dc06ec87d9ee1f5ad0cafbdd3b6e968eb3a7179ed920dcbebafbc1698a5` |
| **最终待验收R1** | **`candidate-source-v2`** | **`2777195be55d3eaff594baa57986edce18d1e383376191b0ae54f267aedd5a38`** | **`2dd40dc06ec87d9ee1f5ad0cafbdd3b6e968eb3a7179ed920dcbebafbc1698a5`** |

不要覆盖初版 `candidate-source` 或旧报告。最终兼容保护只改 Python 适配，Rust 扩展不变。当前工作区生产源码已与 `candidate-source-v2` 核对一致。

## 已有测量及尚未裁决的问题

`corpus/manifest.json` 已冻结29份输入：首轮23份，加棋盘格、1000个孤立区域、合并格、超宽稀疏 XLSX、500×10旧XLS和孤立区域ODS。双方只读取同一份字节。

初版五轮 AB/BA 位于 `cumulative-pair-01`，29份完整输出均相同。初测表格压力场景约19%–50%提速，但这些成绩属于初版，不作为最终v2验收结论。

初版棋盘格 XLSX 的50 ms采样 RSS 比率约1.173，需对最终v2重新测量，并按需要使用独立父进程高频采样复核；不得直接忽略或以汇总收益抵消。

## 恢复顺序

1. 检查工作区/分支、当前源码和扩展摘要，确认没有新的外部修改；保留 N0–N6 和本批全部改动。
2. 用 `reference-source` 对 **`candidate-source-v2`** 运行29份输入、五轮交替基准，输出新目录 **`cumulative-pair-02`**。最终全量测试已通过，无新修改时不必重复全量。
3. 对耗时或 RSS 超5%的样本反序复测。棋盘格 RSS 应重点核查；高频外部采样脚本可由上一轮 `scripts/measure_external_rss.py` 复制适配到R1，必须指向最终快照和最终累计报告。
4. 运行 `R1/scripts/measure_first_parse.py`，显式传 `--candidate-source .../candidate-source-v2 --cumulative .../cumulative-pair-02 --manifest .../corpus/manifest.json`。它计入 parse 内真实首次 native 加载，计时前不调用 `backend_info` 预热扩展。
5. 正式性能任务结束后，运行 `R1/scripts/verify_nonpdf_exports.py`。显式传 reference/candidate 路径、`corpus/manifest-exports.json`；共34份输入，21份非压力案例，双方合计294次七格式实际导出。随后核对 bundle、PDF像素及代表性 PDF/HTML 视觉。
6. 构建/安装协议37 wheel，独立验证 Python 3.14；再验证3.11，按同一3.11依赖集合比较基线/候选和安装包完整输出。
7. 完成 R1 报告、更新分批计划并单列本机/跨平台范围。R2/R3及B5不属于当前恢复步骤。

正式最终基准命令（从本工作区运行，目标目录必须不存在）：

```sh
/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python tools/benchmark_native_pair.py \
  --baseline-root /Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R1/reference-source \
  --candidate-root /Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R1/candidate-source-v2 \
  --manifest /Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R1/corpus/manifest.json \
  --rounds 5 \
  --output /Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R1/cumulative-pair-02
```

## 安装与最低 ABI 的准备状态

仅准备了命令，尚未执行 R1 wheel/ABI 验收。`R1/scripts/prepared-install-commands.json` 和 `python311-environment-survey.json` 保留相关路径；恢复时以本文最终v2及 `cumulative-pair-02` 为准。

- 复用上一轮 `scripts/verify_nonpdf_wheel_install.py`，显式 `--evidence .../R1 --expected-protocol 37 --expected-cases 29 --manifest .../R1/corpus/manifest.json --reference-report .../R1/cumulative-pair-02/1-after.json`。
- Python3.11解释器：`/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B08/venv311/bin/python`；兼容依赖在同目录 `lib/python3.11/site-packages`。
- 3.11先对两份冻结源码做 `--rounds 1` 的同依赖完整输出检查，仅作正确性，输出 `abi311-source-pair`；随后将同一R1 wheel安装到新3.11 venv，使用该pair的 `1-after.json` 作为安装对照。
- 原 `.venv4` 和旧 B8 环境保持只读；新安装环境通过 `.pth` 复用对应版本依赖，不混用3.14与3.11的 site-packages，也不声称重新解析安装全部依赖。

暂停后不自动继续。收到恢复指令后，从上述第1步核对状态再推进。
