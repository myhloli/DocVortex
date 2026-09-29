# PDF 原生内核阶段 22：表格网格复用与栏带扫描降重

## 实现边界

本阶段以 `main@878870f`（包含 Stage 21 合并与开放 Path 修复）为基线，只减少既有 Python 边界的重复工作；私有协议保持 28，不修改 Rust ABI、公开 SDK、`auto|python|rust` 计算选择、`auto|legacy|session` 渲染选择或输出 schema。

表格检测侧新增同一候选上下文的网格连通分量冻结：延迟候选先计算一次 `_connected_rule_grid_components()`，闭合网格检测和 owned merge 只复用该分量重建 bbox。直接调用旧内部函数时仍按原签名即时计算；owned merge 对复用分量执行普通 `_LocalAxisLine` 与有限 bbox 校验，特殊输入仍回退完整参考路径。marker-safe 上下文校验改为单次顺序扫描，同一字符/字典字段不再被多层生成式重复读取，特殊 char/Bbox 仍整体回退。

栏带推断侧保留算法语义，但删除三类重复扫描：只有一个 lane 时短尾不可能跨栏，直接跳过校验和前序扫描；lane 分配前一次排序栏带，`_fits_only_one_lane_ordered()` 不再为每个 bbox 重复排序；最佳/次高覆盖在一次遍历中求得，平局仍保留首个最佳 lane。表格 cell glyph 已由 `_cell_glyphs()` 全局排序，visual row 分组后不再重复稳定排序。自有 `Bbox` 增加 exact-type 快路径，仅当内部是四个有限 float 且坐标正向时直接返回 tuple，反向、非有限、int 或子类仍走原通用转换。

## 正确性

- 32 PDF／299 页公开回放每份双跑；ModelJson、MiddleJson、素材哈希和诊断与 Stage 21 正式候选输出完全一致。
- MinerU 当前 `dev@f504cff`：31 份 eligible Flash 文本完整输出一致；32 份实际 medium shared 完整输出一致。MinerU 工作区仅保留原有未跟踪 `examples/`，未修改其源码。
- Rust/session 完整测试：5472 passed / 14 skipped。
- Python/legacy 完整测试：4762 passed / 724 skipped。
- Cargo workspace tests、Clippy `-D warnings`、rustfmt、Ruff check 均通过；本仓既有 20 个无关测试/工具文件不符合当前 ruff format，未纳入本阶段 patch，改动文件 format check 通过。
- ABI3 wheel 为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`。CPython 3.14.4 独立环境中 DocVortex 核心路径和 MinerU Flash 文本路径的 `auto|rust` 输出一致，协议 28。源码扩展与 wheel 扩展 SHA-256 均为 `bc42f1546006c340cb7175b5bbfa6698641a59057758c046a8f5f4c0bf5a222e`。

## 性能与资源

正式计时均为每文档一次预热、五次热运行，基线/候选执行方向逐文档交替；RSS 使用独立进程树采样。首轮任一样本耗时超过 5% 时均按相反方向复测，复测均无持续退化。结果仅代表本机语料与当前 PDFium 环境。

| 链路 | main 基线 | Stage 22 候选 | 首轮总降幅 | 首轮最大耗时比 | 首轮最大 RSS 比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DocVortex 公开 parse，32 PDF | 16.535675 s | 16.049855 s | 2.94% | 1.19798 | 1.00948 |
| MinerU Flash 文本，31 PDF | 16.379259 s | 15.990359 s | 2.37% | 1.10413 | 1.05150 |
| MinerU medium shared，32 PDF | 19.056479 s | 18.956444 s | 0.52% | 1.26994 | 1.04256 |

公开 parse 中 `demo4.pdf`、`mixed_text_layout_sample.pdf.xor` 和 `quarterly_report_financial_tables.pdf` 首轮耗时触发复测，反序比例分别为 `0.94217`、`1.04183` 和 `0.98873`，无持续退化。Flash 中 `demo4.pdf`、`annual_report_research_projects_table.pdf` 和 `fund_asset_and_transaction_tables.pdf` 触发复测，反序比例分别为 `0.96987`、`0.93490` 和 `1.01703`，无持续退化。shared 中 `mixed_elements_pages_03_06.pdf`、`small_ocr.pdf` 和 `pollutant_discharge_tables.pdf` 触发复测，反序耗时比例分别为 `1.00299`、`0.98000` 和 `0.95873`，无持续退化。

一轮 32 PDF cProfile 中，本轮针对的累计热点下降如下：

- `_detect_table_candidates()`: `6.821288 s → 4.258895 s`
- `_materialize_table_blocks()`: `5.383532 s → 3.768801 s`
- `_infer_text_lanes()`: `3.251807 s → 2.474491 s`
- `_build_rule_table_candidates()`: `3.227190 s → 2.065146 s`
- `_merge_owned_table_candidates()`: `2.223635 s → 1.366970 s`
- `_reattach_cross_lane_short_tails()`: `1.293215 s → 0.971536 s`
- `_connected_rule_grid_components()`: `0.851147 s → 0.487159 s`
- `_cell_visual_lines()`: `0.786876 s → 0.455115 s`
- `_prepare_marker_line_context()`: `0.615293 s → 0.249741 s`

原始 2 倍性能目标仍未完成。Stage 23 需重新从最终 cProfile 选择，主要候选包括 `_detect_table_candidates()` 约 `4.259 s`、`_materialize_table_blocks()` 约 `3.769 s`、`build_document_geometry_plan()` 约 `3.435 s`、`detect_pdf_text_script_lines()` 约 `2.385 s`、`_infer_text_lanes()` 约 `2.474 s`，以及 geometry canonical 样本的 `metadata()`／`_plain_source_records()` 剩余打包成本。不应把正确性通过或本阶段局部热点下降表述为 2 倍目标完成。

证据目录为 `output/pdf/native-kernel-20260930-stage22/`。本阶段不自动合并、发版或发布 PyPI。
