# DocVortex 分批优化实施计划

> 创建日期：2026-10-23  
> 分析基线：`main@b2ef8d02`，DocVortex 0.6.0，必需 Rust 后端，私有协议 36。  
> 当前状态：2026-10-24，B0、B1、B2、B3、B4、B6A、B6B 已验收；B7A 已完成缓存与 CI 正确性验证，整体 CI 提速未证实。B7B 已验收并保留同平台 wheel 复用；用户指定本轮不实施 B5；B8 累计验收已完成。结果见 [B0 报告](optimization-b0-report.md)、[B1 报告](optimization-b1-report.md)、[B2 报告](optimization-b2-report.md)、[B3 报告](optimization-b3-report.md)、[B4 报告](optimization-b4-report.md)、[B6A 报告](optimization-b6a-report.md)、[B6B 报告](optimization-b6b-report.md)、[B7A 报告](optimization-b7a-report.md)、[B7B 报告](optimization-b7b-report.md)和 [B8 报告](optimization-b8-report.md)。

## 1. 目标与执行原则

本轮优先减少已经确认的重复计算、导出复杂度和额外内存占用，同时修复性能验证工具的缺口。保持公开 API、输出协议、解析语义、图片质量和资源所有权契约一致。

推荐顺序为 **B0 → B1 → B2 → B3 → B4 → B5 → B6A → B6B → B7A → B8**。B7B 是有条件开展的 CI 后续项；先完成 B7A 的数据采集，再决定是否实施。每批独立修改、验证和记录结果，不把多个批次合成一个无法归因的性能结论。

执行要求：

1. 所有新增函数、方法及测试辅助函数均有中文注释或文档字符串。
2. 本地 Python 可使用 `/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python`。每次先确认实际导入的源码、原生扩展路径、协议及依赖版本，不能只凭当前目录判断。
3. 本轮沿用 native-only 策略；不恢复 Python 计算后端，不改变已退休环境变量的行为。
4. 纯性能优化不调整版面规则、阈值、文字顺序、表格结构、字体、分页或图片清晰度。发现算法缺陷时单独记录和处理，不混入等价优化。
5. 不因速度变快而接受未解释的输出差异，不通过刷新金标掩盖回归。
6. 每批完成后保留独立代码边界。提交、推送、合并、发布分别记录状态，执行测试不代表完成这些操作。
7. 实施前重新检查工作区；保护已有未提交修改。本文创建时已有以下四个未跟踪文件，不纳入本轮批次，除非后续明确扩大范围：
   - `tests/unittest/test_flash_corpus_recheck.py`
   - `tests/unittest/test_flash_recheck_report.py`
   - `tools/flash_recheck_report.py`
   - `tools/recheck_flash_corpus.py`

历史优化文档中的旧协议、旧后端、两倍提速目标或 130% RSS 上限不自动适用于本计划。本计划采用第 4 节的独立验收标准。

## 2. 已确认的优化点与证据

以下数据来自 2026-10-23 当前工作树的只读调查和轻量测量，用于定位成本。它们不是优化前后正式对照，不能直接转化为预期提速百分比。正式证据需在 B0 后按统一口径重新建立。

| 编号 | 优化点 | 已确认的事实 | 主要位置 | 对应批次 |
| --- | --- | --- | --- | --- |
| O1 | 配对基准缺少跨版本完整输出校验 | 仅校验 before、after 各自多轮稳定；指纹只含 ModelJson。稳定但不等价的候选仍可生成性能比例 | `tools/benchmark_native_pair.py`：`_worker`、`main` | B0 |
| O2 | 独立 RSS 汇总仍依赖旧后端标签 | 当前版本比较只产生 `rust-reference`、`rust-current`；`pdf_memory.ratios()` 仍索取 Python 标签，可复现 `KeyError: 'python-current'` | `tests/benchmarks/pdf_memory.py`、`compare_pdf_revisions.py` | B0 |
| O3 | 导出路径关系校验为 O(N²) | 1,001 / 3,001 / 10,001 条路径分别约 0.0226 / 0.1880 / 2.1377 秒 | `src/docvortex/export/middle.py`：`_validate_export_path_relationships` | B1 |
| O4 | 同一字符视图重复计算 CJK 度量 | 17 页 `nougat.pdf` 单次 parse 调用 206 次，累计扫描 743,968 个字符，全部返回 False；三次累计耗时 0.128 / 0.141 / 0.133 秒 | `src/docvortex/analyzers/native/pdf/table_filled_grid.py`：`has_inflated_native_cjk_metrics` | B2 |
| O5 | 同一根 Form 裁剪范围重复遍历 | 同一样本每次调用 30 次，对应 15 个不同的页内根 Form，每个计算两次；累计耗时 0.136 / 0.115 / 0.121 秒 | `src/docvortex/document/pdf/native_objects.py`、`form_structure.py` | B3 |
| O6 | 新建 MiddleJson 再次整树复制 | 10,000 个纯文本块、无图片的物化仍耗时约 0.0557 秒，tracemalloc 额外分配峰值约 16.57 MiB | `src/docvortex/api.py`：`postprocess`；`export/_images.py`：`_materialize_images` | B4 |
| O7 | 覆盖导出将旧产物全部保存在内存 | 真实覆盖 4 × 8 MiB 文件，tracemalloc 额外分配峰值约 32.13 MiB | `src/docvortex/export/middle.py`：`_commit_export_files` | B5 |
| O8 | 路径格式识别对 PDF 做无必要的全文件读取 | `demo1.pdf` 为 336,919 字节，路径入口将全部字节交给 OLE 探测后才确认 PDF；字节入口已提前检查 PDF 签名 | `src/docvortex/document/detection.py` | B6A |
| O9 | 选页重写重复打开原 PDF 计算映射 | 成功重写后再次打开原文件，只为重新求保留页号；轻量调用记录中选两页共构造 5 次底层文档，其中原文件打开 3 次 | `src/docvortex/document/source.py`、`document/pdf/pdfium.py` | B6B |
| O10 | CI 多个环境重复从源码构建扩展 | 测试、Python 中间版本、Pydantic 下界、平台产物矩阵共 13 个源码安装环境，另有 ARM64 wheel 构建；没有显式 Cargo 缓存步骤 | `.github/workflows/ci.yml` | B7A / B7B |

证据解释：

- O4/O5 使用 `tests/unittest/pdfs/flash_layout/annotated/nougat.pdf`。完整 parse 三次分别为 1.994 / 2.207 / 2.027 秒；CJK 检测约占 6–7%。这只是当前成本占比，不能承诺全部消除，也不能简单累加两个热点的收益。
- 该 PDF 的三次边界计时输出，其 ModelJson、MiddleJson、素材摘要和诊断均与未包装基线相同。正式性能计时仍应移除分析包装和 cProfile。
- O6/O7 的数字是 Python 分配追踪结果，不等于操作系统 RSS，也不等于完整进程树峰值。
- O8 针对公开 `guess_suffix_by_path()`；当前 `parse(path)` 在读入字节后使用字节识别入口，不能把这项成本泛化为所有解析调用。
- O9 已确认重复打开的调用链，尚未证明其端到端耗时占比。
- O10 是配置层面的优化机会；实际构建耗时、缓存命中率与关键路径收益尚未测量。

## 3. 批次安排与依赖

| 批次 | 目标 | 前置条件 | 建议提交边界 | 风险 |
| --- | --- | --- | --- | --- |
| B0 | 修复基准门禁，冻结基线和语料 | 无 | 基准工具及针对性测试，不修改生产算法 | 低 |
| B1 | 消除导出路径双重遍历 | B0 验收 | 路径检查及冲突回归 | 低 |
| B2 | 共享同阶段、同字符视图的 CJK 统计 | B0；按推荐顺序在 B1 后 | 字符统计、调用方接入及失效验证 | 中 |
| B3 | 复用页内 Form extent | B0；按推荐顺序在 B2 后 | 页面提取范围内结果共享及生命周期验证 | 中 |
| B4 | 独占新建文档原地物化 | B0；按推荐顺序在 B3 后 | 内部物化路径及输入不可变验证 | 中 |
| B5 | 用磁盘备份降低覆盖导出内存 | B0、B1 | 导出事务实现及失败恢复回归 | 较高 |
| B6A | 强签名有界读取，保留既有入口优先级 | B0 | 格式探测及输入读取验证 | 低至中 |
| B6B | 删除选页映射的重复打开 | B0 | PDF 重写结果和页号映射回归 | 中 |
| B7A | Cargo 缓存与 CI 构建耗时记录 | 当前功能批次稳定 | CI 缓存配置，不削减兼容性矩阵 | 低至中 |
| B7B | 有条件复用同平台 ABI3 wheel | B7A 数据证明仍有明显重复构建成本 | 单独 CI 调整，不与缓存配置混交 | 中 |
| B8 | 完整回放、累计性能及安装验证 | 本轮拟交付的实施批次全部验收 | 验收报告；发现修复则回到所属批次 | 中 |

生产代码各批在逻辑上不必全部相互依赖，但默认串行推进，避免重复变更污染基线。静态阅读和方案审查可以并行；正式性能测试不得与构建、全量测试或其他重负载任务并行。

## 4. 所有批次共用的验收规则

### 4.1 基线、身份与样本

- B0 完成后，冻结同一份基准工具和共同生产代码基线。初始生产代码基线为 `b2ef8d02`；若实施前工作树已有变化，明确记录新的基线及差异，不静默替换。
- 每批同时保留“上一已验收批次 → 当前候选”的局部对照。B8 再执行“共同生产基线 → 最终候选”的累计对照。
- 记录完整提交号、源码摘要、dirty diff、Python/依赖版本、扩展路径/SHA256/协议、输入路径/SHA256/页数及运行配置。
- 两个源码版本各自使用匹配的原生扩展。即使协议号相同，也不能把另一份源码生成的二进制当作本版本构建证据。
- 性能比较期间不 bump 产品版本；完整输出含 `producer.version`，不应通过临时忽略字段消除版本差异。最终版本变更单独验收。
- 冻结输入集合；缺失文件、输入内容变化、空样本集合或实际导入路径错误必须中止，不得跳过后宣称全量通过。

### 4.2 正确性

- 比较完整 ModelJson、MiddleJson、素材路径及字节摘要、诊断；不删除坐标、空字段、顺序或嵌套内容来迁就结果。
- 格式识别另外比较 bytes/path 两个入口及实际格式结论；选页另外比较源页号、损坏页诊断及 fallback。
- 导出另外比较主文件与全部旁文件、引用完整性、覆盖策略以及异常后的文件状态。
- PDF 分析、几何或图像相关批次检查代表页面的原文、前后标框和最终导出；完整输出哈希与视觉抽查互为补充。
- 涉及 PDF 渲染时，在固定依赖/字体/参数下检查 ORIGINAL 和 REFLOW 的字节、诊断、页面尺寸、文字、链接和像素结果。已有工具的确定性处理需要记录，不新增过滤规则来掩盖差异。

### 4.3 性能和内存

- 热运行每个版本至少五次，报告逐样本中位数及整体统计；AB/BA 平衡执行顺序。首次调用单列。
- cProfile、调用计数、输出序列化/校验与正式计时分开。额外诊断只用于解释结果。
- RSS 使用独立进程测量同一时刻的进程树总量，不累加不同进程各自发生在不同时间的峰值。记录采样间隔及可能漏掉瞬时峰值的限制。
- 对耗时或 RSS 超过基线 5% 的样本反序复测；持续退化先定位、调整或回退，不用总收益抵消单样本问题。
- 对微秒/毫秒级入口使用足够重复次数，并报告绝对时间，避免按一次调用噪声判定回归。
- 各批必须证明其针对的重复工作、复杂度或分配确实下降；不要求每份 PDF 都提速，也不提前承诺固定总加速比例。
- B1/B5 必须单独计时写出路径。现有 `pipeline.py` 的 render 计时和 `pdf_render.py` 不包含完整的文件事务成本，不能替代导出验收。

### 4.4 失败与回退

- 输出不同、对象别名污染、句柄生命周期错误、回滚失败或持续性能退化，均不能把该批标为已验收。
- 保留失败日志和产物，记录原因；修复后复测受影响检查，不删除首次失败记录。
- 一批无法形成可验证收益时可保留调查结论并撤销该批实现，不以“代码已重构”代替完成。
- 回退限定本批改动，不重置共享工作区或覆盖无关修改。测试基线有既存失败时单列，先确定是否影响本批结论。

## 5. B0：统一性能基准与冻结基线

### 目标与范围

修复 O1/O2，使后续每一批的性能结论都以输入一致、完整输出等价为前提。优先复用现有工具，不新增一套相互独立的输出指纹规则。

主要文件：

- `tools/benchmark_native_pair.py`
- `tests/benchmarks/rust_pdf.py`
- `tests/benchmarks/compare_pdf_revisions.py`
- `tests/benchmarks/pdf_memory.py`
- `tests/test_benchmark_entrypoint.py`
- `tests/benchmarks/README.md`

### 实施清单

- [x] 为配对计时复用或提取 `rust_pdf.public_once()` 的完整输出采集逻辑，覆盖 model、middle、assets、diagnostics。
- [x] 同时检查各版本内部稳定性、两个版本之间的等价性和输入身份；不等价时保留差异证据并以失败退出，禁止生成“已通过验收”的性能结论。
- [x] 删除独立 RSS 汇总中已失效的 Python 比较对；比例计算与反序复测使用同一原生比较定义。
- [x] 保留 `pdf_memory.py --worker` 的现有可用路径，验证 standalone 和 worker 两个入口。
- [x] 补充能够拦截真实遗漏的反例：单边稳定但跨版本不同、仅 middle 改变、仅素材改变、仅诊断改变、缺少测量标签、输入不一致。
- [x] 验证基准恢复运行只复用身份匹配的记录；更换源码或扩展后不得误用旧输出目录。
- [x] 补充导出专用工作负载，覆盖路径预检、真实写盘、同内容复用、不同内容覆盖；计时边界和 RSS 分开记录。
- [x] 核对基准依赖：在基准 README 明确安装 `psutil`，未改变产品运行依赖。
- [x] 固定语料清单、共同生产基线、基准工具版本和输出目录约定，执行真实 PDF 的完整链路冒烟。
- [x] 更新基准说明：`pdf_render.py --backend python/accel` 控制 ReportLab `_rl_accel`，并非恢复 DocVortex Python 计算后端。

### 验收与交付

已知反例全部被拦截；当前 `rust-reference`/`rust-current` 报告可完成独立 RSS 汇总及阈值触发的复测。保存基线身份、语料清单、真实样本报告和基准命令。B0 只改变验证工具，生产输出不变。

实际验收：115 项聚焦测试通过；真实 public/shared、五轮配对和独立 RSS 复核通过；27 组导出场景输出等价；10 份 worker 记录续跑复用且未改写。共同基线与 34 份/338 页输入已冻结，尚未做整个集合的完整性能回放。证据保存在 `/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B00`，详细身份和命令见 [B0 验收报告](optimization-b0-report.md)。

## 6. B1：导出路径检查改为按父路径查找

### 实施方案

在 `src/docvortex/export/middle.py::_validate_export_path_relationships()` 中构建规范路径集合，逐条检查其各级父路径是否被某个输出文件占用，避免所有路径两两比较。

不把字符串前缀误当成目录关系，例如 `images/a` 与 `images/ab.png` 不冲突；保持路径规范化、平台差异、符号链接和输出根目录检查的现有行为。

### 执行与验证

- [x] 梳理 `write_artifact`、`save_bundle`、`export_middle_json` 三个入口对路径检查的使用。
- [x] 覆盖正常同级文件、祖先文件占目录、插入顺序变化、相似前缀、多级目录以及重复输入路径的既有行为。
- [x] 运行已有 `tests/unittest/test_middle_json_export.py`、`tests/test_export_image_names.py`、`tests/test_export_html_images.py`、`tests/test_public_export_api.py`。
- [x] 用 1,000 / 3,000 / 10,000 条路径复测校验耗时，并运行带大量旁文件的真实写出任务。
- [x] 比较全部文件内容、冲突类型和副作用；保留现有符号链接检查。

**完成标准：** 两两遍历被消除，增长趋势明显改善；正常输出和冲突处理等价，公共导出性能无持续退化。提交范围只包含路径算法及对应验证。

实际验收：133 项聚焦测试通过；10,000 条浅层路径由 2.189 秒降至 9.663 毫秒；27 组真实写盘场景双向输出等价。一个初始耗时回退场景经过固定路径、固定及默认 hash seed 的十对专项测量未再复现，原始结果保留。13 页真实 PDF 的 106 个导出文件全部相同。证据和完整边界见 [B1 验收报告](optimization-b1-report.md)。

## 7. B2：共享页内字符度量统计

### 实施方案

围绕 `has_inflated_native_cjk_metrics()` 建立当前阶段、当前字符视图的统计证据，让表格检测、图形处理与相关恢复规则复用。先消除重复扫描；是否把计算迁移到 Rust，留到复测后决定。

主要涉及 `table_filled_grid.py`、`table_detection.py`、`graphics.py`、`native_cjk_bands.py`，以及实际承载字符视图的页面准备结构。

### 执行与验证

- [x] 列出调用点、字符列表来源、几何修复发生点和过滤子集，确定统计的有效阶段。
- [x] 对同一有效视图仅计算一次，缓存 False 结果；不同子集或修复后的几何独立计算。
- [x] 不使用只按 `id(chars)` 且跨调用长期驻留的全局缓存；明确失效与释放方式。
- [x] 保持 CJK 范围、至少 20 个锚点、三个中位数和全部判定阈值不变。
- [x] 覆盖无 CJK、少量 CJK、失真字体框、正常中文、过滤子集、字符几何修改及重复调用。
- [x] 运行相关 `test_flash_pdf_tables.py`、`test_flash_table_grid_repairs.py`、`test_pdf_cjk_objects.py` 和实际受影响规则测试。
- [x] 在 `nougat.pdf` 及中文异常度量样本记录调用次数、字符访问量、阶段时间、parse 总时间和 RSS。
- [x] 对表格、图形和中文混排页面执行完整输出差分及视觉审阅。

**完成标准：** 同一阶段同一视图的重复全量扫描被消除；视图变化不会复用过期证据；完整输出等价。不要把“206 次下降到页数”作为硬指标，合法子视图仍需独立统计。

实际验收：2019 项聚焦测试通过；nougat 的完整扫描由 206 次降到 49 次，61 页异常中文样本由 1272 次降到 183 次，正式 parse 中位耗时分别下降约 6.1% 和 9.7%。37 份/443 页完整输出相同，另 1 份扫描 PDF 的拒绝合同不变；六页前后标框像素与两种 PDF 导出的字节、诊断一致。具体生命周期、短页反序复测及视觉覆盖见 [B2 验收报告](optimization-b2-report.md)。

## 8. B3：页内共享 Form 裁剪范围

### 实施方案

在 `_extract_native_page` 的单次页面提取作用域内，使 form bbox 提取和 form structure 提取复用 `_clipped_form_extent()` 的结果，包括 `None`。

主要文件为 `document/pdf/_document.py`、`native_objects.py`、`form_structure.py`。第一版只共享计算结果，不同时重写全部对象树遍历。

### 执行与验证

- [x] 核对当前页面、根 Form、页框和旋转参数，建立明确的作用域和键。
- [x] 在页关闭前释放缓存；不把裸 PDFium 指针或借用对象带出页面生命周期。
- [x] 直接调用相关底层提取函数时仍能正确运行，不强制调用方提供内部缓存。
- [x] 保持现有异常隔离、无 clip 返回值、空可见范围、遍历顺序和最大深度规则。
- [x] 覆盖裁剪/无裁剪、重复 Form、嵌套矩阵、旋转、空范围、损坏对象、连续打开关闭不同文档。
- [x] 运行 `tests/unittest/test_pdf_form_containers.py`、`tests/test_pdf_object_bridge.py`、`tests/test_rule_pixel_form_acceleration.py` 及实际受影响测试。
- [x] 比较 `form_bboxes`、`form_infos`、完整模型输出及素材；对 Form 样本检查前后视觉区域。
- [x] 显式纳入 `tests/unittest/pdfs/form_containers/research-report.pdf` 和 `2022.emnlp-main.614.pdf`。当前默认 `corpus all` 不含这两份文件，不能把默认集合通过当作 Form 语料已覆盖。

**完成标准：** 相同页面提取中相同参数的根 Form extent 最多计算一次；生命周期与输出一致，真实样本确认重复成本下降。若进一步把 bounds 合并迁入 Rust，应另列后续批次。

实际验收：523 项相关测试通过；nougat、研究报告、EMNLP 论文的完整 extent 计算分别由 30/16/36 次降至 15/8/18 次，叶子访问同步减半。五个样本热运行中位耗时下降约 1.6%–3.6%，RSS 无持续回退；首次调用的单点偏慢经六对 AB/BA 测量未复现持续退化。37 份/443 页完整输出相同，扫描 PDF 拒绝合同不变；六页标框像素及两种 PDF 导出一致。证据、生命周期和首次调用复测见 [B3 验收报告](optimization-b3-report.md)。

## 9. B4：减少新建 MiddleJson 的无必要复制

### 实施方案

公开 `materialize_middle()`、render 和 bundle 保持输入隔离；仅为 `postprocess()` 内部刚创建并确认独占的 MiddleJson 提供原地物化路径。

主要文件为 `api.py`、`export/files.py`、`export/_images.py`。现有 `owned_render_document` 已避免 render planner 再次深复制，不能重复计算这部分潜在收益。

### 执行与验证

- [x] 检查新建 MiddleJson 与输入 ModelJson、extensions、嵌套内容是否仍存在共享可变引用，先证明独占边界。
- [x] 将图片物化的核心遍历与公共防御性复制边界清晰分开，公开签名和行为保持一致。
- [x] AssetStore 的索引隔离与不可变字节共享继续保留。
- [x] 覆盖纯文本、嵌套块、图片、表格 HTML 图片、重复素材、`keep_model_json=True`、错误退出和重复调用。
- [x] 验证输入 ModelJson、外部 MiddleJson 和已有素材索引不被修改。
- [x] 运行 `tests/test_document_envelope.py`、`tests/unittest/test_postprocess_pages.py`、`tests/test_refactor_rendering.py`、`tests/test_public_pipeline.py`、`tests/test_public_export_api.py`、`tests/test_export_image_names.py`、`tests/test_export_html_images.py`；特别核对 metadata/extensions 的深复制契约。
- [x] 对 10,000 块纯文本和图像密集文档，分别记录分配峰值、RSS、postprocess 耗时及端到端表现。

**完成标准：** 内部新建文档少一次整树复制，公开输入隔离和异常语义不变；分配下降由实测支持。不要扩大为任意可变文档共享或全局渲染结果缓存。

实际验收：210 项测试通过，内部 MiddleJson 复制由 1 次降至 0 次，公开物化/render/bundle 均保留 1 次。一万段正文的物化分配峰值由 16.57 降至 0.70 MiB，postprocess 中位耗时下降约 24.9%；1000 个图片引用样本的 postprocess 分配下降约 28.9%。37 份/443 页 PDF 与追加的 10 份/36 页跨格式输入输出一致，扫描 PDF 拒绝合同不变。真实 PDF 无超过门槛的持续退化，六页标框和两种 PDF 导出一致；阶段口径、RSS 反序复测及完整边界见 [B4 验收报告](optimization-b4-report.md)。

## 10. B5：覆盖导出改用磁盘备份

**状态：本轮未实施。** 2026-10-24 用户明确指定跳过；以下仅保留原方案，清单不表示本轮待完成。B8 已核对 `_commit_export_files()` 与共同基线完全相同。

### 实施方案

在 `_commit_export_files()` 中分块比较已有内容，并使用同文件系统临时备份支持异常恢复，避免把所有旧产物保存在 `originals` 字节字典中。新输出仍由调用方持有；本批减少的是旧内容备份造成的额外内存。

### 执行与验证

- [ ] 明确预检、写临时文件、备份、替换、恢复、清理的执行顺序与所有权。
- [ ] 全部冲突预检完成前不替换目标文件；同内容重复写出仍可复用，`overwrite=False` 仍拒绝不同内容。
- [ ] 备份与对应目标位于可执行原子替换的同一文件系统，唯一命名并维护清理记录。
- [ ] 第二个或后续文件提交失败时恢复已替换文件，并删除本次新增文件。
- [ ] 恢复本身失败时保留可恢复备份与清晰错误信息，不能清理掉唯一剩余旧内容。
- [ ] 覆盖已有/新增文件混合、符号链接、目录冲突、临时写失败、备份失败、替换失败、恢复失败和清理失败；分别检查原始错误、旧文件恢复状态与临时文件残留。
- [ ] 故障注入按目标文件和提交阶段定位。备份方案可能增加 `os.replace` 次数，不再依赖“第二次调用一定是第二个文件提交”的偶然实现顺序。
- [ ] 运行 `tests/unittest/test_middle_json_export.py` 的既有覆盖与回滚回归，并覆盖 RenderArtifact.write、save_bundle 的集成入口。
- [ ] 用 4 × 8 MiB 及更大素材集合，独立测量真实 overwrite 的额外分配、RSS、时间和磁盘临时空间。

**完成标准：** 旧内容备份不再使 Python 内存随旧文件总字节数线性增长；文件内容、冲突处理和可恢复性符合现有契约，无持续写出性能退化。

**边界：** 当前事务是逐文件替换与异常回滚，不是整个目录的单步原子切换。本批不宣称新增断电一致性或多进程并发写入保证；若需要这些能力，另立方案。

## 11. B6A：减少格式识别的全文件读取

### 实施方案

使路径入口先读取有界文件头并识别 PDF 等强签名；OLE 探测先检查 magic，命中后再进入容器解析。保留 ZIP 容器身份验证与扩展名回退的既有语义。

### 执行与验证

- [x] 以当前 bytes/path 的既有格式合同为准核对优先级，特别检查带尾部 ZIP 数据等可能同时命中多种探测的输入，不能只移动分支后假定等价。
- [x] 普通 PDF 路径识别不再完整读入文件后调用 OLE 探测。
- [x] 覆盖 PDF、OLE、OOXML、ODF、EPUB、OFD、RTF、MHTML、HTML、CSV/TSV，及错误扩展名、伪造头部、损坏包。
- [x] 运行 `test_guess_suffix_ooxml.py`、`test_guess_suffix_ole2.py`、`tests/test_magika_runtime.py`，并执行相关格式识别回归。
- [x] 对普通大 PDF 记录实际读取字节量、首次/热识别时间与内存；不声称清空操作系统缓存，不将识别收益报告为 parse 的整体收益。

**完成标准：** 强签名路径的多余全文件读取消失，普通输入和既有边界测试结论一致。若发现 bytes/path 原有优先级冲突，先明确并单独记录语义决策，不能作为无行为变化优化悄然修改。

实际验收：428 项测试通过；19 份、76 页、129 个素材的完整解析输出及 metadata 相同，另一个 64 MiB 附件 PDF 的识别结论也相同。普通 PDF 路径读取降至固定的 262,452 字节；64 MiB 压力样本识别耗时 4.794→0.160 ms，分配峰值 64.128→0.191 MiB。保留 PDF/MHTML 头与有效 ZIP 尾包的既有 bytes/path 优先级差异；OLE 使用无缓冲句柄避免整读时额外复制。HTML 初测超过门槛、反序复测未持续退化。详情与中间修正记录见 [B6A 验收报告](optimization-b6a-report.md)。

## 12. B6B：选页重写直接返回页号映射

### 实施方案

让重写实现直接返回实际使用的标准化页索引，并沿用 `PdfiumRewriteResult`；删除成功重写后仅为映射再次打开原 PDF 的步骤。第一版保留既有输入文档所有权，跨层复用已打开文档可后续再评估。

### 执行与验证

- [x] 保持公开返回字节的辅助入口与原有 `PdfiumRewriteResult` 契约。
- [x] 确保映射对应实际导入的页面，不能直接信任未经归一化的用户参数。
- [x] 覆盖连续页、不连续页、倒数页、重复页、越界、空选择及原有输入校验行为。
- [x] 覆盖常规重写失败、跳过损坏页、无可用页、返回原文 fallback，检查 `used_original`、保留页和损坏页信息。
- [x] 验证调用者传入的 PDFDocument 不被误关闭，内部创建的文档在成功和异常路径均释放。
- [x] 运行 `tests/unittest/test_pdf_document.py`、`tests/test_public_pipeline.py`、`tests/test_document_metadata.py` 及选页相关测试。
- [x] 用相同文档和选页条件记录底层文档打开次数、选页阶段时间、总时间和 RSS。

**完成标准：** 成功重写路径至少删除一次仅用于重算映射的原文档打开；输出、元数据和源页号映射一致。总时延收益按实测记录，不预设比例。

实际验收：268 项测试通过；正常选页准备和 parse 的底层文档构造 5→4 次、原文打开 3→2 次。45 个选页场景与 B6A 一致，其中 42 个成功场景共 97 页、141 个素材；预期 OCR 拒绝与非法范围错误也相同。五份样本的准备耗时下降约 1.4%–19.8%，完整解析与 RSS 基本持平。六个代表页的前后重写/标框像素及 16 页最终 PDF 导出均相同；原始重写 PDF 的随机 ID 和创建时间差异单独保留并验证为既有行为。详情见 [B6B 验收报告](optimization-b6b-report.md)。

## 13. B7：CI 构建优化

### B7A：先增加缓存与测量

- [x] 收集代表性 CI 运行中依赖安装、Cargo 编译、测试及关键路径耗时；区分冷缓存和命中缓存。
- [x] 设计缓存键，包含 OS、架构、Rust 工具链、Cargo.lock、目标 triple、构建 profile 和影响 ABI/编译的配置；源码变化由 Cargo 正确增量重建，避免旧扩展被直接当作新构建产物。
- [x] 保持当前 Python、平台、Pydantic 下界、实际安装及原生 ABI 验证矩阵。
- [x] 记录缓存命中后的实际编译结果、扩展来源和 CI 关键路径，确认是否真正减少重复成本。
- [x] 缓存异常时可回到干净构建，不允许以缓存命中代替编译和安装验证。

**完成标准：** 缓存键与产物身份正确，兼容性覆盖不变，冷热运行数据可比较；未观察到收益时保留结论，不声称优化已见效。

实际结果：独立验证分支的同一提交完成三轮 17/17 CI，缓存命中为 0/15、13/15、15/15；最后一轮产品构建复用 19–20 个依赖 crate，仍重新编译三个工作区 crate，并核对安装产物来源。45 项本地聚焦及 170 项独立安装检查通过，源码失效和缓存清理后的实际重建也通过。Windows 编码问题已修复并保留失败记录。部分阶段持续受时延波动影响，任务跨度为 534/1095/691 秒，整体 CI 提速未证实；本批结论限定缓存正确性与成本观测，详情见 [B7A 验收报告](optimization-b7a-report.md)。代码已推送到[草稿 PR #38](https://github.com/myhloli/DocVortex/pull/38)，未合并。

### B7B：可选的同平台 ABI3 wheel 复用

仅当 B7A 后构建仍占明显成本时开展。按平台/架构构建 wheel，再在不同 Python 和依赖环境中独立安装验证，避免重复构建相同二进制。

- [x] 核实 ABI3 构建配置和最低 Python；同平台复用不等于跨平台复用。
- [x] 安装测试从 wheel 获取扩展，不回落到 checkout 或 editable 安装。
- [x] Pydantic/ONNX Runtime 等依赖下界测试继续真实安装对应版本，不能被 wheel 共享改变。
- [x] 核对 wheel、扩展、提交及缓存身份；实际安装 smoke、ABI 与平台输出校验仍执行。
- [x] 以关键路径和失败排查成本决定是否保留，不仅比较构建次数。

**完成标准：** 相同覆盖下 CI 成本下降且产物来源可审计。若无净收益，标记“未实施，原因已记录”，不阻塞其他优化交付。

实际验收：两轮同提交 20/20 CI 通过，保留原有 17 个验证任务并增加三个 wheel 生产任务；产品构建从 14 次降为 4 次，每轮 13 个独立安装验证全部通过，依赖与解释器版本保持一致。三平台共 189 个实际输出文件在两轮中均与 B7A 逐字节相同。构建/安装命令合计为 328/289 秒，B7A 为 823–937 秒；runner 占用总和为 3156/3400 秒，B7A 为 3960–4372 秒；任务执行跨度为 487/526 秒。保留该方案，但不把 hosted runner 观察当作固定端到端提速保证。68 项聚焦检查及两个独立环境各 491 项检查通过，详情见 [B7B 验收报告](optimization-b7b-report.md)。代码已推送到基于 B7A 的[草稿 PR #39](https://github.com/myhloli/DocVortex/pull/39)，未合并。

## 14. B8：累计验收与交付

- [x] 所有拟纳入交付的批次各自已有通过报告；被回退或未实施的批次明确标注。
- [x] 对共同基线和最终候选运行冻结完整 PDF 语料，显式包含 B3 的两份 Form 样本，比较完整输出及逐文档时间/RSS；扩大语料时两个版本使用相同集合。
- [x] 对涉及公共 PDF 对象或共享接口的变更运行 shared 回放，使用同一份冻结区域输入；这不替代真实 MinerU 集成验收。
- [x] 对后处理、素材和导出变更运行多格式 pipeline、实际文件写出和 bundle 回读。逐项核对 pipeline 的 `render_errors`，不能把两个版本产生相同错误或脚本退出成功当作格式渲染已通过。
- [x] 对代表性的原文/标框/HTML/PDF 做视觉审阅，涉及 PDF 输出时同时覆盖 ORIGINAL/REFLOW。
- [x] 最终树运行完整 pytest、Python 静态检查和现有 CI 要求；涉及 Rust 变更则运行 cargo fmt、clippy、core tests 并重新构建匹配扩展。
- [x] 构建最终 wheel，独立环境安装并核对来源、CLI、公开 API、真实文档输出；按当前项目策略完成所需平台/解释器验证。
- [x] 如影响 MinerU 使用的公共 PDF 原语、素材或导出契约，补充当前 MinerU 组合的安装及实际调用验证，分别报告冻结回放与真实集成状态。
- [x] 整理累计性能报告、逐样本退化复测、输出差异裁决、测试结果、产物身份及已知限制。
- [x] 最后检查工作区和交付状态，分别记录提交/推送/PR/合并/发布；本计划本身不触发这些操作。

最终报告至少包含：各批实际改动、结构性工作量下降、局部/累计收益、逐样本最差变化、峰值内存、完整输出与视觉结论、测试与安装状态、剩余机会。未完成的平台或实际集成检查须保留为未完成，不能由本机单测代替。

B8 检查组合后的累计影响，不是首次执行真实语料回放。B2/B3 等影响分析证据的批次必须在本批验收时完成相关语料全字段比较和视觉审阅。

实际验收：37 份/443 页公开 parse 与 shared 完整输出相同；公开热耗时中位数之和下降 5.81%，shared 基本持平。首次调用统一空字节码缓存条件后无持续退化；36 个真实写出场景完成双向五轮比较，1,000 素材耗时下降 5.5%–20.7%。350 次七格式渲染无错误，bundle 回读、HTML/PDF/标框视觉审阅通过。本机安装包全量 11071 项通过，组合 CI 20/20、五平台发行物工作流 8/8，以及当前 MinerU 五个真实案例均通过。完整身份、初次失败、复测及已知限制见 [B8 验收报告](optimization-b8-report.md)。候选已提交至[草稿 PR #40](https://github.com/myhloli/DocVortex/pull/40)，未合并、未发布。

## 15. 推荐验证入口与命令

### 15.1 环境预检

以下命令从本仓库根目录运行。先确认选中的源码已有匹配扩展；`PYTHONPATH=src` 只能选择源码，不能构建扩展。

```sh
DV_PYTHON=/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python
git status --short
git rev-parse HEAD
PYTHONPATH=src "$DV_PYTHON" -c 'import sys, docvortex; from docvortex._compute_backend import backend_info; print(sys.executable); print(docvortex.__file__); print(docvortex.__version__); print(backend_info())'
"$DV_PYTHON" -c 'import psutil; print(psutil.__version__)'
```

### 15.2 聚焦测试示例

按批次挑选相关组，不在每批重复执行全部组。新增回归一并加入对应命令。

```sh
# B0：基准入口。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/test_benchmark_entrypoint.py

# B1 / B5：实际导出、素材命名与回滚。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/unittest/test_middle_json_export.py tests/test_public_export_api.py tests/test_export_image_names.py tests/test_export_html_images.py

# B2：表格与中文相关规则。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/unittest/test_flash_pdf_tables.py tests/unittest/test_flash_table_grid_repairs.py tests/unittest/test_pdf_cjk_objects.py

# B3：Form 与原生对象边界。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/unittest/test_pdf_form_containers.py tests/test_pdf_object_bridge.py tests/test_rule_pixel_form_acceleration.py

# B4：公开流水线、输入隔离及素材。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/test_document_envelope.py tests/unittest/test_postprocess_pages.py tests/test_public_pipeline.py tests/test_refactor_rendering.py tests/test_public_export_api.py tests/test_export_image_names.py tests/test_export_html_images.py

# B6A：格式识别。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/test_detection_bounded_reads.py tests/unittest/test_guess_suffix_ooxml.py tests/unittest/test_guess_suffix_ole2.py tests/test_magika_runtime.py

# B6B：PDF 生命周期、元数据和公开选页路径。
PYTHONPATH=src "$DV_PYTHON" -m pytest -q tests/test_pdf_rewrite_mapping.py tests/unittest/test_pdf_document.py tests/test_document_metadata.py tests/test_public_pipeline.py
```

### 15.3 正式解析性能对照

B0 验收后执行。下列 `DV_REFERENCE_SOURCE`、`DV_CANDIDATE_SOURCE` 需替换为各自已冻结且已构建匹配扩展的真实目录；基准驱动使用同一版本。每次选择新的输出目录，不复用其他源码身份的记录。

```sh
DV_REFERENCE_SOURCE=/absolute/path/to/frozen-reference
DV_CANDIDATE_SOURCE=/absolute/path/to/frozen-candidate
DV_BENCH_OUTPUT=output/optimization-20261023/B02/public-run-01

"$DV_PYTHON" tests/benchmarks/compare_pdf_revisions.py \
  --reference-source "$DV_REFERENCE_SOURCE" \
  --candidate-source "$DV_CANDIDATE_SOURCE" \
  --suite public --corpus all --runs 5 \
  --extra-path tests/unittest/pdfs/form_containers/research-report.pdf \
  --extra-path tests/unittest/pdfs/form_containers/2022.emnlp-main.614.pdf \
  --output "$DV_BENCH_OUTPUT"
```

定向定位可用一个或多个 `--path`，例如 `--path tests/unittest/pdfs/flash_layout/annotated/nougat.pdf`；定向成功不能代替 B8 全量验收。两条 Form `--extra-path` 用于 B3/B8 的相关覆盖，其他批次可按其影响范围选择。

shared 对照使用 `--suite shared`，并通过 `--flash-baseline` 指定同一份已冻结区域输入，先验证基线涵盖所选全部文档。冻结区域可以使用参考版本运行 `tests/benchmarks/flash_pdf.py --corpus all --runs 0 --output <新目录>` 生成；需要额外 Form 文档时，应提供完整显式路径清单。

参数边界：

- `compare_pdf_revisions.py` 支持重复 `--path` 和 `--extra-path`，要求 `--runs >= 1`。
- `flash_pdf.py` 支持重复 `--path`，不支持 `--extra-path`；只要提供 `--path`，就替换默认语料集合，因此不能只写两份新增样本而意外漏掉默认文档。
- shared 基线缺少任何所选输入时必须先补齐，不跳过该文档。
- `flash_pdf.py`、`pipeline.py`、`pdf_render.py` 可使用 `--runs 0` 做功能比较；不要把该参数复制到正式解析计时命令中。

B0 修复后，独立 RSS 复核可使用：

```sh
"$DV_PYTHON" tests/benchmarks/pdf_memory.py \
  --timing-report "$DV_BENCH_OUTPUT/report.json" \
  --output output/optimization-20261023/B02/memory-audit-01
```

shared 的独立 RSS 复核还需传入相同 `--flash-baseline`。检查结果中的标签与输入身份，不只看退出码。

### 15.4 导出与多格式验收

- `tests/benchmarks/pipeline.py`：适合多格式分析、后处理、渲染和完整输出检查。
- `tests/benchmarks/pdf_render.py`：适合相同已物化文档的 ORIGINAL/REFLOW PDF 导出比较；其 `--backend` 是 ReportLab 选项。
- B0 建立的导出专用工作负载：必须计入 `write_artifact` / `save_bundle` / `export_middle_json` 的路径检查和实际提交；分别测新写、同内容重复写、真实覆盖。
- 纯路径复杂度和 tracemalloc 微测只作定位，正式验收同时记录真实文件输出和独立 RSS。

## 16. 证据目录与批次记录模板

建议使用 `output/optimization-20261023/` 保存机器产物，仓库文档保存结论和索引。目录名示例：

```text
output/optimization-20261023/
  B00/identity.json, corpus.json, baseline/, benchmark-tests.log
  B01/focused-tests.log, export-benchmark/, report.md
  B02/focused-tests.log, public-run-01/, memory-audit-01/, visual/, report.md
  ...
  B08/full-tests.log, cumulative/, installed-wheel/, visual/, report.md
```

这些是规划中的产物路径，不表示文件已经存在。`output/` 可能被 Git 忽略；跨机器交接时，应将所需证据复制到持久可访问位置或上传为 CI artifact，并在报告中填写位置、保留期限和摘要。

每批完成后填写：

```markdown
### BXX 批次记录

- 状态：未开始 / 实施中 / 待验收 / 已验收 / 已回退 / 未实施（原因）
- 基线提交与源码/扩展摘要：
- 候选提交或 dirty diff 与源码/扩展摘要：
- 基准驱动版本、Python 与依赖：
- 修改范围及实际调用链变化：
- 输入清单与证据位置：
- 聚焦测试结果：
- 完整输出和视觉检查结果：
- 工作量指标（扫描、遍历、复制、打开次数等）：
- 逐样本耗时、RSS、触发的反序复测与结论：
- 相对上一批收益 / 相对共同基线收益（若已测）：
- 失败记录、回退条件和剩余限制：
- 提交 / 推送 / PR / 合并 / 发布状态：
- 后续批次及遗留问题：
```

## 17. 当前进度

- [x] B0：基准门禁、RSS 汇总和共同基线（2026-10-24，见验收报告）。
- [x] B1：导出路径检查（2026-10-24，见验收报告）。
- [x] B2：CJK 统计共享（2026-10-24，见验收报告）。
- [x] B3：Form 裁剪结果共享（2026-10-24，见验收报告）。
- [x] B4：独占 MiddleJson 原地物化（2026-10-24，见验收报告）。
- [x] B5：本轮未实施（2026-10-24，用户明确指定跳过；既有覆盖导出事务保持原样）。
- [x] B6A：路径格式探测。
- [x] B6B：选页映射复用。
- [x] B7A：CI 缓存及耗时测量（缓存与 CI 正确性验证完成，整体提速未证实）。
- [x] B7B：同平台 ABI3 wheel 复用（2026-10-24，见验收报告）。
- [x] B8：累计回放、安装验证和交付报告（2026-10-24，见验收报告）。

## 18. 本轮暂不安排的方向

- 裁图跨批调度和 worker 阈值：存在批大小与 worker 数量可能不匹配的线索，但尚未证明并发后的净收益。保留为调研项，先测 IPC、PDFium 工作量、吞吐和 RSS，再决定是否另立批次。
- 大规模继续 Rust 化：本轮先消除重复工作。若某段仍是明确热点，再选择连续计算阶段迁移，避免仅增加细碎 Python/Rust 往返。
- 重命名目录、全面拆分模块、泛化缓存框架和依赖整体升级：当前没有足够证据将这些工作排在已确认热点之前。

本轮按用户指示跳过 B5，B8 已完成累计验收。后续如重新安排 B5，应单独开始该批。执行时先核对当前代码与进度，再按该批范围推进，保留独立验收记录。
