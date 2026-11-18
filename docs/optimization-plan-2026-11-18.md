# DocVortex Rust 重构与 Python 热点迭代计划

共 **8 轮**，含 1 轮基线、6 轮开发和 1 轮累计验收。R0–R2 建立基线并交付有公开入口对照的改动，R3 共享字符映射，R4 依据剖析收口在框匹配，R5 和 R6 分别调整电子表格和 PDFium 桥接。

起点为 `main@8757b728260c28ccbee070ceb8b317c54cb9c653`（v0.6.24，Rust 私有协议 45）。下文引用的数字来自 2026-11-18 在 macOS ARM64、CPython 3.14.4、pypdfium2 5.13.0 上的临时实验。2026-11-18 已按用户选择执行本地验收，实际结果与试验取舍见[迭代验收报告](optimization-results-2026-11-18.md)。每项改动与上一已验收版本配对，R7 再直接与 R0 比较。任何输出差异都在引入它的那一项定位和处理。

## R0　冻结共同基线

- 固定提交、Python 依赖、实际导入的源码和原生扩展摘要。固定输入字节、解析参数、HTML 来源上下文及 PDF 渲染后端。若实施时主线已前进，记录新起点并重测焦点样本。
- PDF 焦点集选取论文、财报表格与含 Form 的研究报告。同时冻结仓库全量 PDF 清单，分别记录成功解析和预期拒绝的输入。[demo1](../demo/pdfs/demo1.pdf) · [财报表格](../tests/unittest/pdfs/native_pdf_tables/quarterly_report_financial_tables.pdf) · [中文论文4](../demo/pdfs/中文论文4.pdf) · [研究报告](../tests/unittest/pdfs/form_containers/research-report.pdf) · [语料发现](../tests/benchmarks/pdf_corpus.py)
- 非 PDF 使用现有真实文档和压力输入，补入第二本 EPUB。图像接口单列固定随机种子的灰度、RGB、RGBA 输入和线段坐标。[非 PDF 清单](../tests/fixtures/nonpdf_performance_manifest.json) · [输入生成器](../tools/prepare_nonpdf_corpus.py) · [第二本 EPUB](../demo/ebook/epub_02.epub)
- **完成条件**是同一基线可重放，ModelJson、MiddleJson、素材名及字节摘要、diagnostics 四项一致，并留下逐文档热耗时和独立进程树峰值 RSS。正式对照采用无剖析探针的公开 `parse(..., keep_model_json=True)`，交替 A/B、B/A 运行五轮，序列化在计时外。分阶段剖析单独执行。[配对基准](../tools/benchmark_native_pair.py) · [PDF 阶段剖析](../tools/pdf_public_profile.py)

## R1　减少 PDF 表格的重复计算

1. 先处理空候选。图形集合为空时，跳过仅用于排除误认图形的数字表格探测。表格候选为空时，在全页字符校准、规则线和矩形转换前返回。正式表格探测继续使用自身的绘图线和排除区域。[页面准备](../src/docvortex/analyzers/native/pdf/pipeline.py) · [表格物化](../src/docvortex/analyzers/native/pdf/table_materialization.py)
2. 单独优化两处组内中位数。数值表字形已按中心 Y 排序，墨迹锚点已按基线排序。每次追加后从中间位置取值，保留偶数项平均、相等坐标的稳定顺序和原有分组阈值。[数值表组行](../src/docvortex/analyzers/native/pdf/_table_recovery/aligned_numeric.py) · [墨迹基线](../src/docvortex/analyzers/native/pdf/_table_recovery/ink_geometry.py)
3. 两项各自对照，再测组合。上轮四份焦点 PDF 的独立实验中，第一项使端到端中位耗时下降 2.6%–10.3%，第二项下降 2.1%–3.6%。验收覆盖无表页、多表页、图表混排、旋转表头及跨页续表，并回放完整 PDF 语料。逐项核对单元格文字、次序、bbox、归属和 `continues_prev`。[墨迹回归](../tests/unittest/test_native_pdf_table_ink_geometry.py) · [旋转表头回归](../tests/unittest/test_native_pdf_table_rotated_headers.py)

## R2　交付三个可独立测量的 Python 优化

| 实施顺序 | 要做的改动 | 验收 |
| --- | --- | --- |
| 1. EPUB | 内部切片直接消费本次已校验的 Span，复用已有裁剪逻辑。公开入口继续严格校验，返回的样式和链接保持可独立修改。[Span 切片](../src/docvortex/content/spans.py) | 两本真实 EPUB 分别做完整解析和 HTML 导出对照，覆盖嵌套样式、链接、空白裁剪，并回归使用同一投影器的 HTML/MHTML。先导实验中两本 EPUB 的解析耗时分别下降 8.1% 和 10.2%。 |
| 2. CSV/TSV | 仅让确定无需转义的 ASCII 字段按长度扣除 UTF-8 字节预算并写出。保留特殊字段的既有计费和转义顺序。[CSV 渲染](../src/docvortex/analyzers/native/csv.py) | 对照普通 ASCII、中英混合、引号、换行、控制字符及预算恰好用完和首次超限的输入，核对 HTML 字节和异常。10,000×10 的 ASCII 压力样本先导结果为 133.1→35.1 ms。[CSV 回归](../tests/unittest/test_flash_csv.py) |
| 3. 批量绘线 | 默认非抗锯齿路径只创建一次独立 Pillow 画布，按原顺序画完整批线段。[图像实现](../src/docvortex/foundation/_image_numeric.py) | 逐像素比较灰度、RGB、RGBA、空线段和非连续输入，核对返回数组与输入相互独立，同时跑抗锯齿分支回归。1024×1024 灰度图、200 条线的接口耗时先导结果为 14.95→0.80 ms。[图像回归](../tests/test_image_numeric.py) |

三项分别保存补丁和对照结果。EPUB、CSV 报公开解析耗时，绘线报图像接口耗时。R1、R2 完成后即可独立交付第一批改进。

## R3　统一 PDF 页内字符身份

- 脚本识别和几何风险分析先共享同一份原字对象到快照位置的映射，按页按需创建。上轮 `demo1` 的 13 页每页都创建了两份内容相同的映射，每份合计 45,973 项。[页面收集](../src/docvortex/analyzers/native/pdf/pipeline.py) · [脚本证据](../src/docvortex/analyzers/native/pdf/inline/scripts.py)
- 用一个未经修改的内部行消费者试用快照成员索引。区分快照位置、PDF 原始字符编号和去重后的来源集合。旋转、几何修复或重组产生的字符副本继续使用已有适配路径，公开快照的可变副本保持隔离。[原生快照](../rust/docvortex-python/src/snapshot.rs) · [公开快照](../src/docvortex/document/pdf/snapshot.py)
- **完成条件**是同源页的脚本、几何两个消费者合计至多建一份身份映射，过滤、旋转、连字和关闭文档后的结果通过对照。分别记录字符字典物化时间、页内映射规模、公开解析时间和峰值 RSS。上轮的 80.8% 指字符几何物化占 `prepare_visual_evidence()` 的比例。按页面及整份文档的实测结果选择 R4 的消费阶段。[快照回归](../tests/test_native_text_snapshot.py) · [连字回归](../tests/test_ligature_spaces.py)

## R4　收紧表格计算的 PyO3 边界

1. 先在内部调用方跳过空集合的框重叠匹配，再让普通有限浮点框在一次遍历内完成校验和提取。四份焦点 PDF 的 93 次调用中有 67 次一侧为空，其余 26 次均为普通浮点。大整数、非有限值和自定义数值对象沿用精确语义。[表格探测](../src/docvortex/analyzers/native/pdf/table_detection.py) · [重叠计算绑定](../rust/docvortex-python/src/rule_graphics.rs) · [特殊数值回归](../tests/test_native_table_edge_semantics.py)
2. 用 R1、R3 后的剖析决定是否扩大范围。若表格页仍主要耗在逐字构造 `_PendingGlyph`、再构造 `NativeTableGlyph`，选一条同源路径，把筛字、组行、分词接成一次批量计算。Python 消费行、词元和来源索引。按现有合同处理公开可变输入、墨迹校准和特殊坐标。[字形绑定](../rust/docvortex-python/src/table_pending.rs) · [表格文字恢复](../src/docvortex/analyzers/native/pdf/_table_recovery/text.py)
3. 对照词元、来源索引、旋转表头、表格 HTML 及失败后的文本投影，同时验证 Flash 和 MinerU 共享文字/表格入口。只有真实表格文档的端到端耗时或峰值 RSS 出现可重复改善，才纳入第二项。若证据不足，本轮交付第一项并继续 R5。

## R5　按电子表格调用方的需要计算区域结果

- XLSX 选择 gap 时只取获选区域，随后在富文本及媒体物化后按最终语义筛选。ODS 继续消费原生层计算的 `kept_indices`。普通调用直接移出获选区域，诊断调用保留三个 gap 候选及各自成员。[Rust 区域选择](../rust/docvortex-core/src/spreadsheet_regions.rs) · [XLSX 消费方](../src/docvortex/analyzers/native/office/spreadsheet/projector.py) · [ODS 消费方](../src/docvortex/analyzers/native/office/odf/table.py)
- 先采集真实 XLSX/ODS、密集大表、稀疏表及合并格的峰值 RSS，再做改动。核对评分、平局选择、成员顺序、富文本、图片以及诊断模式的完整候选。验收同时报告省掉的计算、复制和实际 RSS 差值。[区域选择回归](../tests/test_native_spreadsheet_regions.py) · [格式适配回归](../tests/unittest/test_spreadsheet_sparse_adapters.py)

## R6　归并 PDFium 字符 ABI 描述

- 让两条 Python 字符读取路径共用当前 14 个符号的名称、顺序、签名与校验逻辑。Rust 在入口将通过校验的地址转换为有名称的函数字段。保持函数强引用、同库约束、PDFium 锁、页面生命周期及各入口原有的失败行为。[快照桥](../src/docvortex/document/pdf/snapshot_bridge.py) · [字符读取桥](../src/docvortex/document/pdf/text/_pdfium_bridge.py) · [Rust 接收端](../rust/docvortex-pdfium/src/lib.rs)
- **完成条件**是两条读取路径对正常页、空页、签名不匹配和关闭句柄的行为分别与基线一致，真实字体/字形提取及现有平台 ABI 检查通过。本轮按维护性与兼容性验收。[桥接回归](../tests/test_pdfium_bridge.py) · [ABI 验证工具](../tools/verify_native_abi.py)

## R7　累计回放与安装验收

- 用 R0 原始基线直接对照最终候选。完整回放冻结的 PDF 与非 PDF 清单，分别汇总真实文档、压力样本和图像接口。对代表性 PDF 实际导出 Markdown、HTML 和带版面框的 PDF，检查表格边界、页序、字形及素材。对 EPUB、电子表格检查链接、表格和媒体。涉及共享入口的改动再用同一批输入复核 MinerU。[Flash 回放](../tests/benchmarks/flash_pdf.py) · [MinerU 共享入口回放](../tools/mineru_native_replay.py)
- 对本轮涉及的 Rust 代码运行工作区测试、格式和 Clippy 检查，累计候选运行完整 Python 测试。用匹配提交构建的 wheel 在 CPython 3.11 和 3.14 的独立环境做实际解析，按现有 CI 和 wheel 平台矩阵核对同一候选的结果。[PDF wheel 验证](../tools/verify_native_wheel.py) · [非 PDF wheel 验证](../tools/verify_nonpdf_wheel.py) · [现有构建矩阵](../.github/workflows/native-wheels.yml)
- **完成条件**是逐文档列出完整输出对照、热耗时、峰值 RSS、真实导出与安装结果，并记录各轮采纳或搁置的改动。沿用仓库的复测口径，任一样本耗时或 RSS 回退超过 5% 时反序复测，保留绝对值并处理持续退化。累计收益取自最终版本的直接对照。[既有基准口径](optimization-plan-2026-10-25.md)

本地开发可使用 `/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python`。新增函数和方法配中文注释。修改 Rust 的轮次均重建匹配扩展。若 Python 开始依赖旧扩展不具备的接口或行为，同步更新私有协议与加载校验。
