# R2 RTF Rust 组范围扫描验收报告

日期：2026-10-25。基线为 R1 最终已验收源码，DocVortex 版本保持0.6.1，私有原生协议由37升级至38。

**R2 本机验收通过。** 七份 RTF 的公开解析热耗时中位数之和下降 **32.92%**，真实 RTF 下降 **50.90%**；34份累计输入的完整输出一致。完整测试12170项通过，39份导出输入、308次实际写出通过，同一 ABI3 wheel 在 Python3.11/3.14 的实际安装和公开入口验证通过。

## 1. 选定边界

在 R1 最终源码上重新 profiling 后，组范围发现仍需生成大量最终被丢弃的正文 token。真实 RTF 的 `_group_spans` 累计约占该次 cProfile 总时间70%，2000段落压力输入约33%。这些比例受 profiler 开销影响，仅用于选择迁移范围，不能作为公开解析收益。

本批选择一次 bytes 到全部闭合子组偏移的接口，保留正文 lexer、parser、代码页、样式、列表、公式、图片和语义投影。没有同时重写正文 token 消费，也没有跨文档缓存。

## 2. 实现与语义边界

- 纯 Rust 核心：`rust/docvortex-core/src/rtf_groups.rs`。一次扫描完成控制词、数字参数、转义、十六进制、二进制载荷及资源限制校验，仅返回按闭合顺序排列的组范围。
- PyO3 边界：`rust/docvortex-python/src/rtf_groups.rs`。借用强引用持有的不可变 Python bytes，在纯扫描阶段释放 GIL；返回偏移或带格式/资源分类的错误，由 Python 构造原异常类型。
- Python 接入：`rtf/parser.py::_group_spans` 恢复原 `_GroupSpan` 类型和共享原始 buffer。destination 的256字节前缀识别、首组选择、前导信息解释和正文解析继续使用原实现。
- 保留子组闭合顺序、`direct_only`、未闭合组和多余右括号后的根组恢复。二进制 payload 内的花括号、控制词和超长参数不进入扫描。
- 保留参数十位限制、32位有符号范围、负二进制长度和截断消息，以及单个 token 的词法错误先于 token 数量错误的检查顺序。前导默认代码页告警仍先于全文扫描错误，字体等解释仍在完整扫描成功后执行。
- `MAX_RECORDS` 和组深度在每次扫描独立计费，合法动态整数预算传给 Rust。自定义 lexer 工厂、构造或迭代方法、ASCII 助手、组 token 类型、参数范围、字节子类和非 bool 选择值保留原迭代路径。

公开 `RtfLexer` 生成器的原实现保持不变，因此直接迭代仍具有原有惰性、位置和异常时机。默认组收集无需创建逐 token Python 对象。Cargo依赖未增加。

## 3. 冻结身份

证据目录：`/Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R2`，下文路径均相对此目录。

| 身份 | 源码/资源/构建配置 SHA256 | 扩展 SHA256 |
| --- | --- | --- |
| R1 `../R1/candidate-source-v2`，协议37 | `2777195be55d3eaff594baa57986edce18d1e383376191b0ae54f267aedd5a38` | `2dd40dc06ec87d9ee1f5ad0cafbdd3b6e968eb3a7179ed920dcbebafbc1698a5` |
| R2 `candidate-source`，协议38 | `0501d52502fadc68d78bb1ca5b14bd9fb7f65730aa7b29c2f576a40576f69486` | `82990fbf2716f65a82b0b151ed98624ecc28f2418487527e6da957b0f6d4a495` |

基线复用 R1 已验收、独立构建并冻结的匹配扩展；R2 实际重新构建，日志见 `build-extension.log` 和 `build-wheel-01.log`。最终 wheel 内扩展与正式计时的字节相同。

## 4. 性能、内存和首次调用

正式热运行在 macOS ARM64、CPython3.14.4下测量。使用无 profiler/函数包装的公开 `parse(..., keep_model_json=True)`，序列化、素材摘要和完整输出比较位于计时外。五轮 AB/BA交替，逐输入独立进程测 RSS，期间未与构建、测试、导出或其它正式基准并行。汇总为逐文档五轮中位数之和。

`corpus/manifest.json` 保留 R1 的29份输入，并补充语义 RTF、长文本、字体/样式表、二进制和大量嵌套样式组，共34份；双方共用一次冻结的字节和摘要。

| 集合 | 基线 | R2 | 耗时变化 |
| --- | ---: | ---: | ---: |
| 七份 RTF | 0.79140 s | 0.53090 s | -32.92% |
| 全部34份输入 | 3.58709 s | 3.34906 s | -6.64% |
| 14份真实综合输入 | 1.80765 s | 1.80323 s | -0.24%，接近持平 |

| RTF 输入 | 基线公开parse | R2公开parse | 耗时变化 |
| --- | ---: | ---: | ---: |
| 真实 RTF | 35.217 ms | 17.292 ms | -50.90% |
| 2000段落 | 210.829 ms | 156.744 ms | -25.65% |
| 语义夹具 | 9.613 ms | 8.831 ms | -8.14% |
| 长文本 | 144.453 ms | 30.921 ms | -78.59% |
| 字体/样式表 | 34.312 ms | 22.021 ms | -35.82% |
| 二进制载荷 | 0.542 ms | 0.456 ms | -15.87%，绝对差约0.086ms |
| 大量嵌套样式组 | 356.431 ms | 294.633 ms | -17.34% |

34份逐样本热耗时和独立 RSS 中位数均未出现超过5%的退化，无需触发反序退化复测。七份 RTF 的 RSS 比率在0.9752–1.0056之间。RSS使用现有50ms线程采样，不能据此声称捕获了全部亚毫秒峰值或每种输入都降低内存。

首次调用另外测量八份真实样本及两份 RTF 夹具，每版三轮独立解释器和空字节码缓存，计时前不调用 `backend_info`，包含 parse 内首次原生加载，排除整个进程启动和预先公共包导入。10份完整输出均一致，最大耗时中位数比率1.0137，未出现超过5%的退化。

证据：`profile-before`、`cumulative-pair-01`、`performance-summary.json`、`first-parse-01`。`abi311-source-pair` 仅用于另一运行时的输出验收，与测试/导出并行，单轮时间及RSS不作为性能结论。

## 5. 差分、真实路径与测试

`tests/_rtf_groups_oracle.py` 冻结 R1 原 lexer，原文件 SHA256 为 `5d1dca956323ef64c535159f4e425e939427929cf41c2869fb3cfefe60b433a2`。预期结果不调用新 Rust 扫描。

差分覆盖22类定向输入、128个固定种子控制流/随机字节组合、两种组选择、预算/深度边界，以及全部65536种两字节十六进制候选；逐项比较组位置、稳定顺序、异常分类和完整消息。公开 lexer 原文身份、自定义词法路径、独立预算、前导告警/错误顺序均有回归。真实 RTF 和语义夹具通过公开入口与冻结组发现的完整输出比较。

`runtime-path-probe.json` 核对七份 RTF 的已完成原生调用和正式基准输出摘要。真实 RTF 完成51次组扫描、2402个组，2000段落完成6次扫描、8010个组；它们包含元数据和正文准备阶段原有的独立子扫描，不能称作整次公开解析只扫描一次。全部探针无原生失败。

- 聚焦回归：271 passed。
- 最终完整测试：**12170 passed、14 skipped、6 warnings**，日志 `full-pytest-01.log`。
- Rust核心：31项单元和8项集成测试通过，日志 `rust-core-tests.log`。
- workspace Clippy全部目标 `-D warnings`、Cargo格式、29份改动/新增Python的Ruff检查和格式，以及 `git diff --check` 通过。
- 新增代码/测试辅助函数的中文说明检查无缺项，记录在 `function-comment-audit-python.json`、`function-comment-audit-rust.json`。

首次Rust单测中，二进制夹具的预期子组偏移误写为12–16，扫描实际正确返回11–15；修正夹具预期后31项单元及8项集成全部通过。此修正发生在最终扩展构建与冻结之前，未修改扫描算法，记录见 `initial-test-correction.json`。

## 6. 实际导出与视觉

`exports-run-01` 对39份输入执行78个独立解析进程，完整ModelJson、MiddleJson、素材及diagnostics均相同。压力输入只比较完整解析，其余22份各实际写出七种格式。

- **308次实际写出成功**，**44次bundle回读和HTML再渲染成功**；逐项核对主文件、旁文件、素材及渲染诊断。
- **22对PDF原始字节相同**，双方各136页的文字、尺寸和像素签名一致。
- 11对DOCX仅ZIP时间字段不同，成员与其它字节相同。新增语义RTF的七种导出原始字节均相同。
- Poppler100dpi另渲染真实RTF第1/2页和语义RTF第1页，六张PNG逐对相同，已查看三张候选页面，未发现新增裁切、重叠、乱码或素材丢失。见 `visual-review/pdf-review.json`。

既有非致命导出诊断前后相同；本批没有修复既有公式回退或远程素材占位，不把相同诊断记作质量改善。导出错误不会因前后相同而算成功；本轮错误为零。

## 7. 安装与交付边界

wheel：`dist/docvortex-0.6.1-cp311-abi3-macosx_11_0_arm64.whl`，SHA256为 `501b057f17233ba0c65ea66bf157987405e2d993bd9a4d7bb77208fadcc2b463`。

同一wheel分别安装到新Python3.14.4和3.11.15环境，实际来源、协议38、原生二进制摘要、CLI、432个包文件及其中431个非原生文件均通过核对。各34份完整输出通过；Python3.11先在同一3.11依赖环境中比较R1/R2冻结源码，再与实际安装包比较。两个运行时各通过公开PDF全页和选页的完整输出及原生入口烟雾验证。

证据：`wheel-acceptance-02`、`wheel-acceptance-311`，各自含 `pdf-smoke/summary.json`。依赖沿用各运行时已有site-packages，未重新解析/安装全套依赖；原 `.venv4` 和B08产品安装未被替换。

首次3.14安装脚本调用漏传已构建wheel路径，脚本按设计拒绝覆盖dist并停止；未进入安装。失败保留在 `wheel-acceptance-01`，显式指定原wheel后的 `wheel-acceptance-02` 通过，没有替换或修改被验收的wheel。

本机验收仅覆盖macOS ARM64的两个Python端点。跨平台CI、提交、推送、PR、合并和发布均未执行，不能用本报告替代这些交付环节。

## 8. 后续和回退

工作区：`/Users/myhloli/.codex/worktrees/nonpdf-optimization/docvortex`，分支 `codex/nonpdf-optimization`。N0–N6、R1和R2修改均保留，原工作区既有recheck文件未纳入本批。

`R2-production.patch` 以冻结R1为边界，单列七个生产文件；`final-working-tree.patch` 保存累计修改。回退R2需恢复R1生产源码和匹配协议37扩展，并同步恢复原生注册/协议测试，不应连带撤销N0–N6或R1。

R2完成后，下一批为R3的OFD表格方向/坐标索引与拓扑计算；文字Delta、矩阵、quad、clip保持独立子批。B5继续跳过。
