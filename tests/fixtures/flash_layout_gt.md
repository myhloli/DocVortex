# Flash PDF 布局标注集

[GT v1.1](flash_layout_gt_v1_1.json.gz) 的 120 个物理页来自 120 份 PDF、113 个文档家族。60 页用于开发，验证和测试各 30 页。历史问题、历史对照、独立常规三个来源分别有 48、36、36 页。每个家族只进入一个拆分。

夹具保存 1,824 个区域、原件与渲染图 SHA256、教师版本指纹以及评分所需的关系与掩码。每个区域最多保留本版使用的两个短文字锚点。PDF 和渲染图由使用方提供。

- `physical_page` 从 1 起算。`bbox` 是可见原页左上原点归一化的 `[left, top, right, bottom]`。
- `scored_fields` 指定可评分维度。`type: null` 或没有类型掩码时跳过细类评估。`exhaustive_types` 标识本页已复核完备、可计冗余预测的类。`table_dimensions` 依次是含表头的行数和列数。
- `before` 的配对为前块、后块。`caption_of` 和 `footnote_of` 为附属块、主体。`outside_bodies` 约束列出的各区域不属于列出的各主体。区域引用均为同一物理页内的 ID。
- `teacher/source_index/source_type` 追溯 PP 或 VLM 的来源。PP 为发布后处理的 1 基序号，VLM 为 0 基序号。`type_teacher/type_source_index` 记录独立裁决的细类来源。

在仓库根目录执行以下命令。第一条验证夹具，第二条按实际文件内容校验源 SHA256 并导出本地重放索引。也可多次传入 `--source-root` 指向原件目录，移动或改名后仍按 SHA256 匹配。

```sh
python tools/verify_flash_layout_gt.py
python tools/verify_flash_layout_gt.py --manifest source-manifest.json --export-index local-replay.json
```

源清单接受 `documents` 下的 `sha256/path`，或 `pages` 下的 `pdf_sha256/pdf_path`。相对路径以清单所在目录为基准。索引输出保留标注，并为每页附加经核验的本地 `pdf_path`。将索引写到本地输出目录。

扩充时沿用冻结的家族拆分，新增原件需记录完整 SHA256 与字节数。更新版本、区域、关系和对应的评分掩码后，用 `--gt new-gold.json` 验证同结构的明文 JSON，再用 `gzip -n -c new-gold.json > new-gold.json.gz` 生成可分发的夹具。升版时保留前一版成绩，并在新版上同时重算基线与候选。

## 对比两个解析版本

评分工具需要 DocVortex 的项目环境和 SciPy。每个重放根目录提供 `documents/<document_id>/middle.json.gz` 或同名的未压缩 JSON，包含完整原件的全部公开页面。有 `status.json` 时还会检查运行成功状态与原件 SHA256。不同来源可以分开存放，重复传入对应目录即可。

```sh
python -m pip install scipy
python tools/score_flash_layout_gt.py --manifest source-manifest.json \
  --baseline-dir baseline-history baseline-regular \
  --candidate-dir candidate-history candidate-regular --output scores.json
```

输出包含每页各维度的评分与分母、按文档家族聚合的各来源指标、测试折结果和配对置信区间。普通页只对 `exhaustive_types` 明确声明的类别计额外预测。明确裁为 `blank_verified` 的物理页出现任何输出块，整页库存项记零。区域几何使用单独的 `bbox` 掩码，无该掩码时仍可借助粗定位找到对应的剩余候选，评价已标类型、锚点或顺序。表体缺少已冻结文字锚点时，从原始 PDF 字符中独立选取唯一文字串。

## 第二轮独立小表复制集

[第二轮小表与近似非表 GT](flash_layout_gt_r2_replication_v1.json.gz) 包含 27 个新家族，每家族一页，`split` 均为 `replication`。小网格 6 页、少线表 7 页、彩色或底纹表 4 页，另有 10 页近似非表对照。17 个正例页共 35 张表。论文、财报、产品资料和表单按目标形态选取，复制成绩按家族及 `stratum` 单列。120 页主集继续提供原有主分。

在仓库根目录校验补充夹具，或使用与主集相同格式的原件清单导出本地索引。

```sh
python tools/verify_flash_layout_replication_gt.py
python tools/verify_flash_layout_replication_gt.py --manifest new-source-manifest.json --export-index replication-index.json
```

源文件匹配使用 `pdf_sha256` 和 `source_bytes`。`render_sha256`、`size_px` 标识实际原图，框使用同一可见页归一化坐标。夹具的 `schema` 是 `flash_layout_gt_replication`，`version` 是 `r2-1`。评分端可以调用 `ReplicationGold.model_validate_json(read_json(path))` 读取扩展字段。[补充夹具读取器](../../tools/verify_flash_layout_replication_gt.py)

- **表体与边界。** 遵循每个区域的 `scored_fields`。若有 `accepted_alternative_bboxes`，同一对象可在主框和各个等价框中取最大 IoU。这里 6 处色带标题允许进入表体或单列。每个目标表匹配一次。`distinct_objects.regions` 所列框分别属于不同对象。
- **行列与单元格。** `table_dimensions.rows` 和 `columns` 各自可选，分别以标了该维度的表为分母。15 张表有 44 条 `cell_assertions`。对每条断言，使用合并单元格展开后的逻辑网格，对照 `column_0based`、`row_0based` 和可见 `text`。行号为 `null` 时，只在指定列里查找。一个行列坐标上的完整值须匹配，单元格文本可规范 Unicode、大小写和空白。行列与单元格命中单独汇报。
- **非表保护。** `protected_non_tables` 引用需要独立保留的同页文字或图形。`outside_bodies` 指明不能吞入哪些表。24 页的 `exhaustive_types` 明确含 `table`，这些页可按完整表库存计算多报。两页开放表单另有 `forbidden_table_combinations`，一张预测表对其中**每一个**指定区域的覆盖率都达到 `min_coverage_per_region`（本版为 0.75）时计一次违规。覆盖率的分母是被保护区域的面积。另一个产品页的单行键值区保留歧义，只对明确标记的三张表做正例评估。

升版时按同一补充 GT 分别重算基线和候选，保留 `annotation_sha256`、`manifest_sha256` 和模型权重指纹。上轮已披露的五个真表与一个符号图属于诊断样本，不计入这 27 个新家族的分数。


独立表格集通过以下命令比较两个版本的完整 `MiddleJson`。结果包含各机制的文档家族宏平均和每项分母。只在明确穷尽标注的页面统计多出的表，保护区按实际覆盖面积检查。HTML 格值按逻辑行列比较，合并格沿其 rowspan 和 colspan 占据的坐标展开。数值的正负号、小数点、单位均参与匹配。

```sh
python tools/score_flash_layout_replication.py --baseline-dir blind-A --candidate-dir blind-C2 --output replication-scores.json
```

## 窄网格局部挑战

[窄网格挑战夹具](flash_layout_narrow_grid_v1.json.gz) 保存两次按原件格线筛查、再看渲染图裁决的局部标注。首次筛查 26 份文档、31 个区域，复核筛查 19 份、20 个区域。两批分别汇总。真数据表要求 IoU ≥ 0.5 且 HTML 可展开出单元格。符号图记录被识别为表和保留为图的次数。允许多种表达的图例与只覆盖部分对象的区域单列。多个区域可能来自一个模板，两个批次也有近似设计，成绩仅用于这个机制。

每条记录包含原件与渲染图 SHA256、物理页和归一化框。`key` 为 PP 的区域序号或原生格线来源 `grid`。复现时使用与主集相同的全文重放目录结构。

```sh
python tools/score_flash_narrow_grid.py --baseline-dir narrow-A confirm-A \
  --candidate-dir narrow-D confirm-D --output narrow-scores.json
```

## 第三轮独立表格复制集

[第三轮小网格、少线表和图形对照](flash_layout_gt_r3_replication_v1.json.gz) 使用同一复制集格式。20 个新家族各选一页，其中 15 页含表、5 页是非表图形对照。31 张表中有 28 张带逻辑行列或选定格值，102 个完整格值逐个由原图核对。89 个图形、说明或标题同时参与区域保护。源页存在明确空白格时，用指定的零基行、列及空字符串标注；预测 HTML 缺格会失分。图注或跨列标题可有不同合法归属时，按区域记录等价边界，或仅核对逻辑列。

29 个目标表区带可抽取的原生文字；另外 2 个来自同一份含原生文字的混合页面，其表区没有文字层。两处的 7 个格值计入完整视觉集合，分析原生格值时另外列出其余 95 项。此专项按原页几何和 PP 候选富集，验证已获两路教师支持的真实对象与近似反例。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r3_replication_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r3_replication_v1.json.gz \
  --baseline-dir baseline-20 --candidate-dir candidate-20 --output replication-r3.json
```

### 编号表题归属补充

第三轮首次盲评分之后，在同一批 20 个家族的原图上追加了题注父子关系审计。[编号表题夹具](flash_layout_gt_r3_caption_audit_v1.json.gz) 覆盖其中 8 页、18 个有独立外框且只能归属一张表的编号题注。题名位于表内、同一题名覆盖多个分面，以及没有编号的分区标题均保留歧义。使用下列命令的 `summary.relations` 比较明确归属的命中数。该补充用于后续回归，原 20 页盲评版本与分数分别保留。

```sh
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r3_caption_audit_v1.json.gz \
  --baseline-dir fresh-A --candidate-dir fresh-D --output caption-relations.json
```

## 第四轮少线表独立复制集

[第四轮少线表、工程格线表与图形负例](flash_layout_gt_r4_replication_v1.json.gz) 包含 20 个新家族的 20 个物理页。14 个少线页有 22 个必需表区，2 个格线页有 2 张工程表，4 个负页有统计图、热力图或实验拼图。139 个格值均按原图核验，含 4 个指定位置的空白格。17 对明确题注父子关系随本版首次标注一同冻结。保留科学记数、千位分隔、显著性星号、尾零与负号；保险费率表的空白表头也必须作为可寻址的空格存在。

19 页穷尽了表类别。意大利财务报表页右侧小结有多种合理的分块，按标记的两个金额区做局部召回与格值评分。天文参数表的三个物理横线端点和右栏正文帮助确定跨栏边界。该页可见下划线的参数名用实物的 `SIDEREAL_DAY` 核验。来源于同一监管表单模板的另一份年报在最终分组前已排除。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r4_replication_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r4_replication_v1.json.gz \
  --baseline-dir fresh-A --candidate-dir fresh-D --output r4-replication-scores.json
```

### 已披露少线表的逐格复核

[开发用逐格夹具](flash_layout_gt_r4_disclosed_dense_v1.json.gz) 在第三轮已披露的 5 份原件、7 个目标表上增加了 279 个原图完整格值。它与第四轮 20 个全新家族的盲评独立计分。多层表头仅在实际有字的起始格作断言，保留小数尾零、正负号、± 不确定度与字形标点。两张英文教材表的内框标题可以保留在表内或移为题注，36 项在指定列内查找完整值；其余 243 项均固定行列。可沿用第三轮的原件清单，用两个版本在这 5 份完整原件上的输出比较。

```sh
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r4_disclosed_dense_v1.json.gz \
  --baseline-dir old20-A --candidate-dir old20-D --output disclosed-dense.json
```

## 第五轮多层表头与正文分节

沿用完整原件、物理页、一对一表框匹配与原有格值评分。单元格可附加 `rowspan` 和 `colspan`，两项须一起提供，行、列取左上角锚点。`summary.cell_span` 只统计明确标注的格，要求锚点、完整文本与两个方向的跨度同时正确。`summary.cell_value` 仍使用原有格值定义。对于分节标题可有不同计行方式的表，可以在区域的 `scored_fields` 中加入 `row_assertions`，再给出多组带列号的完整文本。`summary.same_row` 要求一组内所有值出现在同一逻辑行的不同实际单元格。

| 夹具 | 原件与评分范围 |
|---|---|
| [已披露的四张开发表](flash_layout_gt_r5_disclosed_v1.json.gz) | 4 个家族，418 项格值，其中 29 项另核跨度。59 个完整正文行有独立的同行断言。可用第四轮复制集的原件清单定位。 |
| [新家族复制集](flash_layout_gt_r5_replication_v1.json.gz) | 15 个家族，其中 12 个正页、3 个负页。17 张真表，186 个完整格值，其中 59 项另核跨度。 |
| [短横线定向集](flash_layout_gt_r5_rail_sample_v1.json.gz) | 另外 15 个家族，各取一个由原件横线与 PP 表框共同提名的表区。21 个带内容的跨度和 32 项同行断言，只对指定局部评分。 |

三份夹具分别评分，源家族与抽样分布用于解释结果。在仓库根目录执行以下命令，指向对应两版公开全文产物目录即可复评。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r5_replication_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r5_rail_sample_v1.json.gz \
  --baseline-dir rail-A --candidate-dir rail-K3 --output r5-rail-scores.json
```

## 第六轮正文合并格

本轮继续使用同一逻辑坐标与严格跨度评分。一个组名跨多条记录时只在源格左上角记一次，要求预测在同一坐标拥有它。如果相邻两条记录打印了相同文字且其间有清楚的格线，分别按两个 1×1 格计分。原图明确的独立空格也有自己的锚点。按下表的来源分别计分，保留每组样本的分母。

| 夹具 | 适用范围 |
|---|---|
| [已披露开发原件](flash_layout_gt_r6_disclosed_v1.json.gz) | 5 个家族、6 个表区，542 个带完整文字和精确跨度的源格。含 60 个正文跨行格、5 个正文跨列格、430 个独立正文格及 47 个表头格。位置或跨度有多种视觉解释的格已掩蔽。 |
| [新家族盲集](flash_layout_gt_r6_replication_v1.json.gz) | 17 个家族、38 张真表，251 个格值，226 个精确跨度及 41 个同行断言。保留带真实空白的位置、非原生表区和整篇失败的原件，报告分别给出端到端及可完整配对的分母。 |
| [局部横线定向集](flash_layout_gt_r6_bands_v1.json.gz) | 另外 14 个家族，每份只取一个原图表区。121 个带跨度的源格包含正文跨行和共用值，也含独立空白、相邻重复值、普通数据与表头。 |

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r6_disclosed_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r6_bands_v1.json.gz \
  --baseline-dir bands-A --candidate-dir bands-K4 --output r6-bands.json
```

## 第七轮整篇论文的正文行组

[整篇论文表格夹具](flash_layout_gt_r7_articles_v1.json.gz) 收录七篇不同论文的七张实物表，逐格标明 472 个源格的完整文字、行列锚点和跨度。五张取自已披露家族的全篇回放，两张取自事前冻结的 24 篇独立论文全文回放。该夹具只纳入发生变化且已按原图转录的表，可用于后续定向回归。独立效果按最初选定的全体论文家族计分。

本机 PP-DocLayoutV2 提供整页表框，VLM 对同一原页独立给出布局。源格以整篇论文可见的墨迹和字层为准。训练裁片与同一论文中的实体表有版式或数值差异时，分别保留来源和分母。复核区域表头时沿用原始坐标，旧版遗漏的表头行也在评分范围内。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r7_articles_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r7_articles_v1.json.gz \
  --baseline-dir gt-A --candidate-dir gt-K2 --output r7-article-scores.json
```

## 第八轮表头、正文与题注归属

[表头与题注独立夹具](flash_layout_gt_r8_headers_v1.json.gz) 记录 23 篇不同论文原件的 23 张表和 23 条题注，有 182 个表头格、290 个正文格及 23 个题注归属关系。每个格均使用完整文字、绝对锚点和双向跨度。数据来自在查看新旧 Flash 输出前固定的 24 篇全文，其中一篇没有找到与作者裁片对应的实体表，仍参与整篇输出回放。表框与题注框按原件及原生字层复核，本机 PP-DocLayoutV2 提供区域候选，较难的四页另有 VLM 框作对照。

仓库通用评分器计算原始行列上的命中，并检查题注的父容器。第八轮的独立复核还逐条比对完整题注、导出 HTML 和不应进入表体的相邻文字。这些指标单独记录。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r8_headers_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r8_headers_v1.json.gz \
  --baseline-dir holdout-gt-A --candidate-dir holdout-gt-K3 --output r8-header-scores.json
```

## 第九轮全页表区与相似非表

[独立表区夹具](flash_layout_gt_r9_regions_v1.json.gz) 取自事先冻结的 DocLayNet 220 页复核集。36 个精标区域覆盖 33 个来源家族，其中 22 张表共 218 个带位置和跨度的格，另有 14 处目录、链接列表、图卡等相似非表。表框和格值均可追溯到公开源页。5 张表录入全部实体格，其余按可明确判定的部位计分。采用 `row_from_end: 1` 的断言要求落在输出末行，适用于印刷表头有多种合理计行方式的原件。该夹具只对已标区域计分。

各页的 `source_archive_member` 指向 DocLayNet 1.0.0 附加档案中的原始单页 PDF，`pdf_sha256` 标识取出的文件。可将这些 PDF 集中放入一个目录，以 `--source-root` 核对全部原件。仓库评分器沿用历轮忽略词间空白的计分规则，本轮专项另外核对词界。[DocLayNet 数据集及原件下载](https://github.com/DS4SD/DocLayNet#dataset-structure)

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r9_regions_v1.json.gz
python tools/score_flash_layout_replication.py --gt tests/fixtures/flash_layout_gt_r9_regions_v1.json.gz \
  --baseline-dir r9_regions-A --candidate-dir r9_regions-candidate --output r9-region-scores.json
```

## 第十轮少线金额表

[已披露开发集](flash_layout_gt_r10_sparse_v1.json.gz) 覆盖 13 页和 9 个来源家族。11 张少线目标表来自 6 份财报，另有 5 张同页相邻的控制表。夹具登记了 251 个格值，其中 199 个要求位置与跨度同时正确，另有 32 条同行断言。原件上可见的金额、年份和脚注用于裁决，包括遮盖后字层与实际显示不同的金额。

[新发行人独立集](flash_layout_gt_r10_sparse_holdout_v1.json.gz) 是事前从 10 个发行人选出的 30 张单页中的 12 张精标页，覆盖 23 个表区、64 个带跨度的格值、93 条同行断言及相邻对象保护条件。10 张有原生字层的少线主表单独计分。底纹表、图像表和目录控制各有自己的分母。两份夹具均通过 `source_archive_member` 标识 DocLayNet 官方附加档案中的单页 PDF，并记录其 SHA256。

```sh
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r10_sparse_v1.json.gz --source-root /path/to/DocLayNet/PDF
python tools/verify_flash_layout_replication_gt.py --gt tests/fixtures/flash_layout_gt_r10_sparse_holdout_v1.json.gz --source-root /path/to/DocLayNet/PDF
```

通用评分可沿用本文件第八轮的命令，分别传入对应夹具和按原件摘要索引的两版公开输出。词内空格、货币符同列、源图上的年份与金额归属、跨栏表的阅读顺序，须另外对照原件和实际 HTML 核验。

## 第十一至十三轮的多类型整页与图题关系

[源页精标包](flash_layout_gt_r11_r13_v1.json.gz)保留了 60 个历史页的 67 条局部裁决、30 个独立常规家族的抽样来源及其中 20 页的精标。每页记录原件 SHA256、物理页与源图指纹。常规页保留可接受的替代框、图内关键点、应留在图外的内容、可复制源行、顺序和父关系。历史页的每个字段以本条的计分掩码为准。历史范围中的两条三栏地图问题属于 P2。常规原件通过包内的来源链接定位，下载件先核对字节数和哈希。

另外两个可单独重放的子集包含 8 张 DocLayNet 验证页的 9 组题图与 4 条栏序，以及 5 本含标签 PDF 的 7 组题图。位置是物理可见页左上原点的归一化框；DocLayNet 的原始页号保留官方值，档案内的单页 PDF 按物理第 1 页评分。每条题图分别统计图体是否命中、文字锚点是否唯一、是否归入正确父图。父图要求 IoU 至少 0.50。相同短锚在正文重复出现时，会降低严格分，逐项输出可区分此情况和找错图。

从两个版本各自的完整公开解析结果目录运行[图题与顺序评分器](../../tools/score_flash_figure_relations.py)。每份原件需有对应的 `documents/<sha256>/status.json` 和 `middle.json.gz`，也支持以 `d-<SHA前16位>` 命名的文档目录。

```sh
python tools/score_flash_figure_relations.py --suite doclaynet --baseline-dir baseline/doclaynet --candidate-dir candidate/doclaynet
python tools/score_flash_figure_relations.py --suite tagged --baseline-dir baseline/tagged --candidate-dir candidate/tagged
```

## 第十四轮的图形区域、墨迹与图外文字

[图形 GT](flash_layout_gt_r14_graphic_regions_v1.json.gz)包含三组在原页核验的真值，共 63 页、47 份原件。公开来源组是 8 份整本的 24 个物理页。另两组分别从本机源原语筛选出 19 和 20 份原件，各保留一个目标页。前者关注彩色非矩形面和内部标签，后者也纳入无彩填充与图形印刷易混淆的对照。两组各自保留来源抽样分母；同一局部命中多个图形条件仍只计一次。整本来源以 SHA256 定位。

每条记录的 `boundary_regions` 给出整页归一化可见图体框。`mask_units` 仅适用于同一记录的 `mask`，公开样本中的墨迹窗口使用 PDF 点，另两组的墨迹位置和参考 RGB 使用归一化坐标。另存的图例附件、合理的分图方案、可搜索文字与非图区域用于逐项产品裁决。没有正例图框的页仍有保护检查，不能按“全页无图”解释。

[图体边界评分器](../../tools/score_flash_graphic_regions.py)按一对一匹配在基线和候选的完整公共输出上同时计分。只读可定位到已保存图片的 image_body/chart_body。输出包含 IoU≥0.50 的命中数及逐区域重叠；IoU 低于 0.20 的未匹配项按零统计。另从 MiddleJson 的非图片块统计每条登记文本探针出现几次，分别汇总可检索数、唯一出现数与分母；布局解析失败的页也保留分母。边界、文字、真实图片墨迹和 HTML 各有独立口径。

```sh
python tools/score_flash_graphic_regions.py --suite public --baseline-dir baseline/native --candidate-dir candidate/native
python tools/score_flash_graphic_regions.py --suite opportunity --baseline-dir baseline/local-colour --candidate-dir candidate/local-colour
python tools/score_flash_graphic_regions.py --suite transfer --baseline-dir baseline/local-adjacent --candidate-dir candidate/local-adjacent
```

## 第十五轮的重复位图、图题与跨来源图例

[重复位图真值](flash_layout_gt_r15_repeated_rasters_v1.json.gz)保留 4 份整本的 38 个物理页、40 个明确的前景放置，其中 20 处有从原 PDF 读取的完整图题。同一截图跨页复用按放置计分，家族结果须另列；两张图同页各有独立图题。另存 27 个容易混淆的源指纹与放置位置作为复核信息。背景区域上可能还画有真实图形，不能把这些框内的所有图片都判错。

[独立图形留出真值](flash_layout_gt_r15_graphic_holdout_v1.json.gz)覆盖 8 个新来源的 11 个物理页，包含主图、侧置图例、截图标注、在不同页复用的截图、普通文字和水印对照。可接受单独输出的图例在源真值中明确登记。独立 HTML 正文、实际图片中关键墨迹、Markdown 投影、图题归属有各自的分母。

[重复位图评分器](../../tools/score_flash_graphic_regions.py)按原件 SHA256、物理页和可解引用的图体一对一匹配。重复前景的一个成品需覆盖源图至少 90%，且源图占裁片至少一半。图题检查还要求编号及文字与正确父图唯一匹配。[独立留出评分器](../../tools/score_flash_graphic_holdout.py)读取两版整本实际输出，可指定多个入口目录并复核页级明细。重复图体目录直接包含 `documents`，独立留出目录按 `native` 和 `default` 区分。

```sh
python tools/score_flash_graphic_regions.py --suite repeated --gt tests/fixtures/flash_layout_gt_r15_repeated_rasters_v1.json.gz --baseline-dir baseline/native --candidate-dir candidate/native
python tools/score_flash_graphic_holdout.py --arm A=baseline --arm C=candidate --output comparison.json
```

## 第十六轮主图、图例和图外正文归属

[源图归属夹具](flash_layout_gt_r16_visual_ownership_v1.json.gz)覆盖 6 个独立出版者的 9 个物理页，含内嵌和外置图例、多面板地图、色卡、带相邻正文的图表。以完整原件 SHA256 和物理页检索。每页有原始渲染图 SHA、归一化部件框和 71 个局部像素窗中的对应见证，另有图外文字保护及可在图片或可编辑文本中择一保留的标签。允许一张合规组合图，也允许分别发布无物理墨迹重复的图片。几何、一致的实际墨迹和独立公开文字分开计分。

[归属评分器](../../tools/score_flash_visual_ownership.py)读取整份文档的 `native/documents/<sha256>` 和 `default/documents/<sha256>`。每个回放根有 `runtime.json`，文档目录提供 `status.json`、带页序的 `middle.json.gz`、公开 `document.html`、`document.md` 及其图片资产。不同回放臂可多次传入 `--arm`，同一臂可按同一源码与金标身份分批提供。若另传 `--teachers`，从 `pp` 和 `vlm` 子目录读取独立布局模型的原页预测，核对输入原图 SHA 并另报模型框；模型框不计为实际公开资产。源图窗的参考色和掩码随夹具保存，计分无需 PDF 原件或模型权重。

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r16_visual_ownership_v1.json.gz \
  --arm A=baseline --arm C=candidate --output ownership-r16.json
```

固定 192 页的扩展集、R14 的 63 页图形专项、R15 的 38 页重复位图与 11 页独立留出、R16 的 9 页独立留出按原件 SHA256 与物理页去重后覆盖 312 页。不同夹具的标注范围与计分维度分别使用各自的分母。

## 第十七轮嵌套图形、相邻正文与合法重复放置

[独立图形归属夹具](flash_layout_gt_r17_visual_ownership_v1.json.gz)取自 6 个新出版者的 8 个物理页。含 15 个图组、23 个部件、48 个图内墨迹窗口、27 条图外文字锚点和 2 个可编辑或图内保留的标签。六份官方原件完整回放 99 页，源 PDF 和渲染按 SHA256 复核。两条公开入口均检查可解码、HTML/Markdown 实际引用的资产。Texas Instruments 同一波形源 XObject 在两个物理页有意重用，按不同物理位置核对。

同组部件允许共用一个合格的图体。评分器分开报告边界、真实图片墨迹、图外文字及图片内重复文字。各组内的部件 ID 允许同名，计算时用图组和部件共同定位。附录 [R17 视觉复核](../../output/pdf/flash-layout-iteration-20261121-r17/holdout/REPORT.md)保留源包、教师预测、整个原件的字节比较、原始与修正结果及一处低对比细字的目视裁决。

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r17_visual_ownership_v1.json.gz \
  --arm A=baseline --arm C=candidate --output ownership-r17.json
```

固定 192 页扩展集及 R14 至 R17 图形专项按源 SHA256 与物理页去重后累计 320 页。各专项的评分分母以实际夹具为准。

## 第十八轮近包含图层和独立印刷信息

[独立图形归属夹具](flash_layout_gt_r18_visual_ownership_v1.json.gz)由 6 家出版者的 7 个物理页组成，标注 14 个图组、23 个部件、37 个图内像素窗和 26 条图外文字。先用官方原件的原生位图 XObject、实际绘制顺序、蒙版及位置富集，再以完整原件回放。覆盖低透明度圆角阴影、含内容的照片叠层、真正嵌图、单色正文衬底和同一资源在异位复用。夹具记录可移植的局部像素和蒙版身份，来源链接用于复核完整原件 SHA256。整体图体、重复像素、公开可编辑正文及异位放置分别核对。[R18 预注册与复测](../../output/pdf/flash-layout-iteration-20261121-r18/holdout/REPORT.md)

沿用归属评分器，向两臂传入各自的完整公开解析结果目录及冻结夹具。每个目录需要 `native/documents/<SHA256>` 和 `default/documents/<SHA256>` 及对应 `runtime.json`。教师目录可选，原页预测几何独立计分。固定 192 页和 R14–R18 图形专项含初组时按原件 SHA256 和物理页去重后累计 327 页。

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r18_visual_ownership_v1.json.gz \
  --arm A=baseline --arm C=candidate --output ownership-r18.json
```

### 选型后的独立 reserve 复核

[R18 reserve 夹具](flash_layout_gt_r18_visual_ownership_reserve_v1.json.gz)覆盖另外 3 家出版者的 4 个物理页，标注 8 个图组、11 个部件、16 个图内像素窗及 13 条图外文字。整本回放 74 页，覆盖带不同蒙版的同位光学图层、有透明开孔的投影仪、含独立图文的印刷样张及压平的照片叠层。此夹具使用单独冻结的源摘要、回放根和评分分母。加入这 4 页后累计 331 个去重物理页。[reserve 全文复核与物理页账本](../../output/pdf/flash-layout-iteration-20261121-r18/holdout/final-external/REPORT.md)

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r18_visual_ownership_reserve_v1.json.gz \
  --arm A=reserve-baseline --arm C=reserve-candidate --output ownership-r18-reserve.json
```

### 原独立组两页的并排卡片读序对照

两家原出版者的两个现有物理页补充了顺序标注。三组 17 个并排图文位置分别核对实际 HTML、Markdown 图片次序、可见题签与可编辑文字。两页沿用原组的 PDF SHA256 及物理页键，累计覆盖仍为 331 页。[源顺序和两路完整成品](../../output/pdf/flash-layout-iteration-20261121-r18/holdout/reading-order/REPORT.md)

```sh
order_dir=output/pdf/flash-layout-iteration-20261121-r18/holdout/reading-order
python "$order_dir/score_order.py" --gt "$order_dir/order-gold.json" \
  --arm "R17-Final=$order_dir/R17-Final" --arm "R18-Accepted=$order_dir/R18-Accepted" \
  --output "$order_dir/score-Accepted.json"
```

## 第二十轮局部遮挡、独立嵌图与原生裁剪

[独立出版者前瞻夹具](flash_layout_gt_r20_visual_ownership_v1.json.gz)保存 5 家出版者的 6 个物理页，整本复测覆盖 85 页。夹具在独立审阅者接触任何 R20 候选产物前封存，以完整原件 SHA256 和物理页定位。夹具含 8 个主图组、20 个主图组墨迹窗、4 项图例或嵌图关系及其 7 个像素窗、10 条图外可编辑文字和 1 个源图被遮挡的禁出见证。坐标均相对原 PDF 物理页，RGB 样本和对比掩码随夹具保存。金标按源对象绘制顺序、PDFium 开关后的像素、纸面可见部分和原生文字标定。教师预测只用于单独核对几何。[预注册、源绘制与公开成品](../../output/pdf/flash-layout-iteration-20261121-r20/holdout/REPORT.md)

| 官方原件 | 整本物理页 | 精标物理页和关系 |
| --- | ---: | --- |
| [Eagle Eye Networks《Brand Guidelines》](https://www.een.com/wp-content/uploads/2024/02/EEN-BrandGuidelines-v2.1-20230222.pdf) | 45 | 23 页的新版横幅、仍可见的早绘截图下半部与独立浏览器标签。36 页的相机软蒙版、矢量连线与渐变背景 |
| [Print ePS《Brand Guidelines》](https://printepssw.com/hubfs/02.%20Corporate%20Assets/04.%20PDFs/11.%20Brand%20Guidelines/Print%20ePS%20-%20Brand%20Guidelines.pdf) | 35 | 14 页裁剪混排的矢量框、弧线、屏幕与图外说明 |
| [MV Ticket《Ticket Download Shop》](https://www.mvticket.de/wp-content/uploads/2019/09/TicketDownLoadShop.pdf) | 1 | 1 页真实独立操作截图与装饰底板 |
| [NSW National Parks and Wildlife Service《Southern Kosciuszko Drives》](https://www.nationalparks.nsw.gov.au/~/media/Visitor/Files/PDF/Brochures/southern-drives-kosciuszko.ashx) | 3 | 3 页位图主地图，以及具独立用途的图例和区位嵌图 |
| [BC Parks《Gowlland Tod Provincial Park》](https://nrs.objectstore.gov.bc.ca/kuwyyf/gowlland_tod_trail_middle_map_9233e1f027.pdf) | 1 | 1 页矢量底图、图例和自有比例尺的区位嵌图 |

针对 R19 Release 与 R20 Release 的整本解析目录运行归属评分器。默认入口与显式原生入口各自保存 MiddleJson、HTML、Markdown、真实图件和文档状态。图组通过要求同一合法物理范围内的公开资产含该组所有源墨迹且没有物理重印。图外文字须在同一位置能编辑且仅出现一次。另对 `relationships` 逐项验证同一主图或独立裁片上的完整像素、公开引用与唯一性，对 `forbidden_witnesses` 扫描已解引用的实际图片。禁出匹配和可见正见证分开计分。整本 ZIP 回读、资产字节、逐页变化单独核对。[实际图例与嵌图评分](../../output/pdf/flash-layout-iteration-20261121-r20/holdout/review.py) · [候选入口与禁出、ZIP 核验](../../output/pdf/flash-layout-iteration-20261121-r20/holdout/replay_candidate.py)

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r20_visual_ownership_v1.json.gz \
  --arm R19=baseline --arm R20=candidate --output ownership-r20.json
```

在 R19 的 339 个固定 GT 物理页上，以原件 SHA256 和物理页去重后新增 6 页，得到 345 个固定 GT 物理页。再并入 R19 两个披露后实物回归断言并扣除重叠，覆盖 346 个可复测物理页。整本解析与精标评分各用自己的分母。[物理页账本](../../output/pdf/flash-layout-iteration-20261121-r20/holdout/coverage-physical.json)

## 第二十一轮位图本体与局部矢量容器

[独立前瞻夹具](flash_layout_gt_r21_visual_ownership_v1.json.gz)保存 5 家出版者的 6 个物理页，整本 40 页。每页标一处主图组合和一处独立图件。另有 10 条图外原生文字及 Verve 白屏下被遮挡的旧蓝屏的禁出像素窗。图体必须在同一个公开资产保留局部覆盖与容器外内容。局部像素须在同一物理位置仅出现一次。对同一步骤截图合入一幅图但完整可读的情况，视觉裁决另记组装粒度差异。

| 官方出版者 | 整本物理页 | 精标物理页和关系 |
| --- | ---: | --- |
| [TARGOBANK](https://www.targobank.de/de/amc-content/pdf/dlst-07-11-a/Dokumente%20in%20der%20Banking-App.pdf) | 2 | 1、2 页的局部红框、跨屏引线、屏幕框外按钮和相邻说明 |
| [Sparkasse Neuss](https://www.sparkasse-neuss.de/content/dam/myif/spk-neuss/work/dokumente/pdf/AnleitungenOnline-Banking/Online-Banking-Bedienungsanleitung.pdf?n=true) | 16 | 12 页分层机身与显示内容、独立读卡器及正文 |
| [TransLoc](https://transloc.com/wp-content/uploads/2026/05/TransLoc-Campus-Flyer-OnDemand-2026-CC.pdf) | 2 | 2 页半透明矢量板与实景照片、页面独立二维码 |
| [Verve](https://resources.verve.com/hubfs/Brand-Guidelines/Verve-Visual-Identity-Guidelines.pdf) | 18 | 12 页被覆盖旧显示屏、仍保留的外侧实景和独立电脑画面 |
| [Visit Vatnajökull](https://visitvatnajokull.is/wp-content/uploads/2018/02/%C3%BEjonustub_R%C3%ADkiVatnaj_2018.pdf) | 2 | 2 页地区地图位图和矢量层、拼入雪山照片和独立市镇地图 |

[基线筛错夹具](flash_layout_gt_r21_visual_ownership_difficulty_v1.json.gz)保存 2 个额外物理页，整本 11 页。只用冻结的 R20 公共成品筛查了 5 个登记页。在查看 R21 产物前封存 TransLoc 第 1 页的青色遮罩与旧色像素窗，以及 [National Gallery of Art](https://www.nga.gov/sites/default/files/2025-06/nga-map-english.pdf) 第 5 页在主图和小裁片中重复的 Mall Entrance。两组共 6 本、49 个去重整本物理页和 8 个精标物理页。[筛选分母、源开关与逐条结果](../../output/pdf/flash-layout-iteration-20261121-r21/holdout/REPORT.md)

R20 Release 与 R21 Paper 实际回放两条公开入口。原生成功解析 49 个去重物理页，默认成功解析 38 页。TransLoc 与 National Gallery of Art 默认被判入 OCR，并保留具体路由异常。其余模型、中间协议、HTML、Markdown、JPEG/PNG 与 ZIP 字节一致。附加的局部复印检查读取已链接的全部公开资产，能覆盖相对主图面积很小的 NGA 重复入口。两套夹具持久化源 SHA256、物理页、归一化位置和像素窗。PP-DocLayoutV2 与复杂页本机 VLM 提供单独的模型几何对照。[前瞻正式实物](../../output/pdf/flash-layout-iteration-20261121-r21/holdout/compare-R21-Paper/public-bytes.json) · [困难组正式实物](../../output/pdf/flash-layout-iteration-20261121-r21/holdout/enriched/compare-R21-Paper/public-bytes.json) · [小裁片评分](../../output/pdf/flash-layout-iteration-20261121-r21/holdout/check_local_windows.py)

```sh
python tools/score_flash_visual_ownership.py \
  --gt tests/fixtures/flash_layout_gt_r21_visual_ownership_v1.json.gz \
  --arm R20=baseline --arm R21=candidate --output ownership-r21.json
```

按源 SHA256 和物理页去重后，固定精标累计 353 个物理页，包含本轮 8 个新增独立精标物理页。整本复跑的 49 个新来源物理页另计。[物理页去重账本](../../output/pdf/flash-layout-iteration-20261121-r21/holdout/coverage-physical.json)

[本轮披露的 NWS 成品回归](flash_r21_callout_underlay.json)覆盖同一本 National Weather Service 原件的第 10 页。R17 固定 GT 只标该原件的第 3 页。加入既有和本轮披露成品断言并去重后，总覆盖 355 个物理页，其中固定独立标注仍为 353 页。

## R22–R24　整页角色、续行与独立出版者

[R22 源页角色和先后](flash_layout_gt_r22_source_roles_v1.json.gz) 有 12 个先看原页记录的局部参考页，保留未评区域。R22 共目视 48 个历史页，另看 6 个常规页。只有具备独立参考的字段参与教师和 Flash 的比较。 [34 个已披露条文段界](flash_layout_gt_r22_paragraph_boundaries_v1.json.gz)保存了两份整本中的原页可见片段，跨页条文按当前页实际文字评分。

[R24 整页 GT](flash_layout_gt_r24_pages_v1.json.gz) 有 11 个新出版家族的 22 个物理页，包含前瞻 12 页及 R21 基线筛错 10 页。两组共 475 个布局区域。[必要先后、独立段与对照](flash_layout_gt_r24_text_relations_v1.json.gz) 另含 60 条局部断言和对应源 PDF 的 URL、SHA256。正文来自原页，保留允许的等价关系和复核过的类型掩码。两套整页原始封存、源指纹、逐页成品与双路结果存于 [R24 留出报告](../../output/pdf/flash-layout-iteration-20261121-r24/holdout/REPORT.md)。

仓库评分器按源字补表区锚点、去除重复锚点，原盲评直接使用封存锚点。两页绝对值有差异，22 页候选增量逐页一致。[评分器交叉核对](../../output/pdf/flash-layout-iteration-20261121-r24/holdout/scorer-crosscheck.json)

在本地已有 R24 原件和公开输出的工作区内，可直接从仓库根目录复算新增整页分。

```sh
R=output/pdf/flash-layout-iteration-20261121-r24/holdout/revealed
python tools/score_flash_layout_gt.py \
  --gt tests/fixtures/flash_layout_gt_r24_pages_v1.json.gz \
  --source-root "$R/source" --baseline-dir "$R/A/native" \
  --candidate-dir "$R/Narrow/native" --output /tmp/flash-r24-pages.json
```

[编号续行专项](flash_layout_gt_r24_numbered_text_v1.json.gz) 另附 6 家原件的 SHA256 和来源、23 个抽取物理页及 50 条断言。其中 34 项是完整编号段，16 项保护真标题、独立编号、表内序号和记录字段。前瞻主分、基线筛错主分、编号专项分分别计算。候选揭盲后，这些家族均进入已披露回归集。 [编号专项报告](../../output/pdf/flash-layout-iteration-20261121-r24/numbered-holdout/REPORT.md)

[揭盲后编号续行回归](flash_layout_gt_r24_disclosed_numbered_v1.json.gz)单独保留了四个变页上被拆坏的 12 个原本完整的条目。默认和显式原生两路的 HTML、Markdown 对照均为 R21 12/12、Narrow 0/12。这 12 条只作后续回归。 [完整条目实测](../../output/pdf/flash-layout-iteration-20261121-r24/numbered-holdout/changed-pages/portable-score.json)

## R25　页边成员归属

[R25 独立整页 GT](flash_layout_gt_r25_pages_v1.json.gz) 覆盖 8 个新出版家族的 21 个物理页，共 279 个区域。前瞻组 3 家 7 页，R21 基线筛错组 5 家 14 页。[独立页边关系](flash_layout_gt_r25_margin_relations_v1.json.gz)以实际印刷源成员、相对顺序、角色和真页边对照计分。针对跨页合段，只要开头文字在公开成品仍可唯一找到，其页内 HTML 宿主页变化可以单独裁决。[新家族协议及双路结果](../../output/pdf/flash-layout-iteration-20261121-r25/holdout/REPORT.md)

[历史页边与页内重复对照](flash_layout_gt_r25_historical_marginals_v1.json.gz) 收录 13 个已披露目标或保护页，另有 15 个依据冻结 R21 选定的印刷源区。包含局部断言、源区域和应由哪些 Flash 类型承载。页内再次出现相同文字只作为候选线索。对于表格成员和图形坐标仍应检查实际容器与裁图，专项目标不并入整页总分。

在有本轮原件及回放成品的工作区，可复算 235 个固定布局页的配对结果，或按本轮历史面板重算角色、HTML 宿主及受保护内容。 [235 页评分入口](../../output/pdf/flash-layout-iteration-20261121-r25/acceptance/score_expanded.py) · [历史局部评分](../../output/pdf/flash-layout-iteration-20261121-r25/historical/score.py)

## R26　短编号、标点与同行正文

[维护库 27 个印刷源区](flash_layout_gt_r26_source_margins_v1.json.gz)同时保留短成员、相邻宿主、原件 SHA、物理页和归一化坐标。可用 12 个正文误判、14 个真实页边和 1 个源字体编码歧义页回测首个角色赋值阶段。双联页左右页码以及章间重编号也在保护范围内。 [取样与阶段跟踪](../../output/pdf/flash-layout-iteration-20261121-r26/census/REPORT.md)

[正文标点原位回归](flash_layout_gt_r26_disclosed_punctuation_v1.json.gz)记录一篇论文 12 个公开变页的 13 个标点、源框与左右邻字。针对后续修复，按原稿中标点两侧的顺序检查 HTML 和 Markdown。该组由 R26 变更发现，单列为揭盲后回归样本。 [原页对齐与计分](../../output/pdf/flash-layout-iteration-20261121-r26/acceptance/thesis-review/order-score.json)

[维护库编号与普通题目](flash_layout_gt_r26_disclosed_numbers_v1.json.gz)收录 4 份出版原件的 9 个变页、11 条已披露断言。10 条要求编号在同一公开段落紧接正确内容，另 1 条保护普通数学题的正文身份。 [双路成品依据与重算入口](../../output/pdf/flash-layout-iteration-20261121-r26/acceptance/pack_numbered.py)

[ISO C 与 IBM 的成品结构回归](flash_layout_gt_r26_disclosed_public_structure_v1.json.gz)保存两份标准的 8 条实际断言，覆盖代码可复制性、真标题、同栏段落及跨栏顺序。R21 的 HTML 和 Markdown 均通过，Row 上两种导出均出现退化。 [ISO C 断言与原件](../../output/pdf/flash-layout-iteration-20261121-r26/acceptance/iso-c-review/disclosed-regressions.json) · [IBM 断言与原件](../../output/pdf/flash-layout-iteration-20261121-r26/acceptance/ibm-review/disclosed-regressions.json)

[R26 独立整页](flash_layout_gt_r26_pages_v1.json.gz)来自 7 份完整原件、17 个物理页及 362 个印刷区域。[页边关系与保护](flash_layout_gt_r26_margin_relations_v1.json.gz)在普通前瞻保留 27 项保护，在筛错层保留 17 项错误和 27 项对照。整页主分累计 252 页；局部关系及揭盲后断言按各自分母另计。 [独立标注及成品报告](../../output/pdf/flash-layout-iteration-20261121-r26/holdout/REPORT.md) · [合并评分](../../output/pdf/flash-layout-iteration-20261121-r26/acceptance/expanded.py)

## R27　已有段首的编号归属

本轮用原页判断边栏数字能否接回同一印刷行。维护库 703 份多页原件的宽松几何筛查产生 31 个候选关系，分布在 28 页，涵盖 24 个应接入的段首和 7 个真页码、表格、公式或同行中段反例。同一论文的节选和完整版保留各自物理页，其中四份开发原件单独标记。 [源框与逐项期望](flash_layout_gt_r27_existing_hosts_v1.json.gz) · [维护库裁决](../../output/pdf/flash-layout-iteration-20261122-r27/census/REPORT.md)

五个新出版家族另提供 14 张整页、364 个区域、86 个源端关系。普通前瞻 8 页与基线筛错 3 页进入主分，另 3 张图件与参考文献专题页单列。每个公开入口均保留其真实完成状态，按两臂实际可评的页面配对。 [整页 GT](flash_layout_gt_r27_pages_v1.json.gz) · [源端关系](flash_layout_gt_r27_source_relations_v1.json.gz) · [独立评估](../../output/pdf/flash-layout-iteration-20261122-r27/holdout/REPORT.md)

## R28　混合字体的词序与词界

六个新出版来源提供 13 个物理页、178 个原稿区域和 27 条局部事实。10 个普通前瞻页进入主分，另有 Unicode 双向文字与 Adobe 公式、叠印的 3 张专题页。公开字序同时检查 HTML 和 Markdown 中的词界、标点和宿主。 [整页 GT](flash_layout_gt_r28_pages_v1.json.gz) · [源端事实](flash_layout_gt_r28_source_relations_v1.json.gz) · [独立评估](../../output/pdf/flash-layout-iteration-20261122-r28/holdout/REPORT.md)

维护库的 13 条源区关系取自十月 1,930 个既审页，包括物理页承接到上一页 HTML 宿主的真实段落。已披露开发样本另存 8 条明确的句子、词界与标点断言。韩文试验的 9 条事后核验保留了完整可见文字与源框，供后续区分字体片段被正确插入的新标点打断、真实词界退化两类变化。 [十月维护关系](flash_layout_gt_r28_maintenance_rows_v1.json.gz) · [开发字序及词界](flash_layout_gt_r28_disclosed_glyph_words_v1.json.gz) · [揭盲后九处原稿校验](flash_layout_gt_r28_disclosed_word_seams_v1.json.gz)
