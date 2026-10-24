# B4 验收报告：独占 MiddleJson 原地物化

日期：2026-10-24。本批对照为已验收 B3 → B4，范围与门禁沿用 [分批优化计划](optimization-plan-2026-10-23.md)。

## 1. 结果

B4 已完成本机验收。`postprocess()` 直接物化刚创建且已与输入隔离的 MiddleJson，省去一次整树深复制；公开物化、render 与 bundle 继续先复制调用方文档。

- 实际复制计数：`postprocess` 的 MiddleJson 深复制 **1→0**，公开物化、render、bundle 均保持 **1→1**。
- 一万段正文的物化分配峰值 **16.57→0.70 MiB**；完整 postprocess 中位耗时 **172.21→129.25 ms**，下降约 **24.9%**。
- 1000 个图片引用的样本，postprocess 分配峰值 **4.06→2.88 MiB**，下降约 **28.9%**；该阶段耗时下降约 **0.7%**。
- **210 项测试通过**，包含新增 10 项可变别名、异常与公开接口隔离回归。
- **37 份、443 页 PDF** 完整输出相同，另 1 份扫描 PDF 的拒绝合同不变；追加 **10 份、36 页**跨格式与压力样本也完全相同。
- 六页前后标框像素与三组 HTML/旁文件相同；两种 PDF 导出的字节、诊断、页面尺寸、文字及链接相同，人工检查了 **16 页导出结果**。

生产修改仅涉及 `api.py`、`export/_images.py`。没有修改 Rust、公开签名、协议、版本、图片命名与内容、渲染规划或布局规则。

## 2. 所有权与调用链

已有转换边界负责隔离输入，B4 保留这些必要复制：

| 字段 | 转换层的隔离方式 |
| --- | --- |
| 页面与嵌套块 | `model_json_to_pages()` 首先 `deepcopy(model_json.pages)`，再进行后处理和严格对象化 |
| metadata | `model_json_to_middle_json()` 调用 `metadata.model_copy(deep=True)` |
| extensions | 同一转换函数显式 `deepcopy(model_json.extensions)` |
| 失败页补位 | `postprocess()` 在内部新建 PageInfo，随后重排当前局部页面列表 |
| 素材索引 | 物化核心始终 `assets.copy()`，两个索引独立，不可变 bytes 继续共享 |

`_materialize_images()` 保留原有参数与防御性复制，随后委托新增的 `_materialize_owned_images()`。核心遍历、图片注册、HTML 引用改写、回调顺序和异常传播保持原样。`postprocess()` 的局部 MiddleJson 尚未交给外部调用方，因此可以直接调用此内部核心。

没有添加公开的 `in_place` 开关。`export/files.py` 无需修改；公开 `materialize_middle()`、render、bundle 和 middle export 仍进入复制包装。既有 `owned_render_document` 避免 render planner 再次复制的行为也不变，不把其原有收益计入本批。

新增测试递归检查模型、字典、列表、字段集合等可变对象的身份，证明返回 MiddleJson 与输入 ModelJson、重复调用的结果互不共享可变字段。保留模型时，`result.model_json is model` 的既有合同不变，MiddleJson 仍与其隔离。

后续修改结果的 metadata、嵌套 extensions、Span 样式及深层列表不影响输入；反向修改输入也不影响已返回 MiddleJson。部分图片已成功物化后，后面的非法载荷仍按原异常失败，原模型与传入 AssetStore 不变，修正输入后可重新调用。

## 3. 实际复制审计

针对两份压力样本，在正式计时之外分别调用真实后处理、公开物化、HTML render 和 bundle，记录根 MiddleJson 的 `model_copy(deep=True)`。计数不包含必要的 raw model 或 metadata 隔离。

| 入口 | B3 | B4 |
| --- | ---: | ---: |
| `api.postprocess()` | 1 | 0 |
| `materialize_middle()` | 1 | 1 |
| `api.render(..., "html")` | 1 | 1 |
| `DocumentResult.save_bundle()` | 1 | 1 |

两份样本均得到上表结果，完整输出一致，公开调用后的源 MiddleJson 也保持不变。证据为 `copy-audit-reference.json`、`copy-audit-candidate.json` 及对应脚本、命令和日志。

## 4. 阶段耗时、分配与 RSS

冻结两份独立 HTML 输入，并用 B3 分析后保存原始 ModelJson/诊断：

- `text-10000`：10,000 个正文段落，加 1 个由 HTML title 产生的文档标题，共 10,001 个 raw 块，无图片。
- `images-1000`：直接图片与表格中的图片混排，共 1,501 个 raw 块、1,000 个物化素材引用，包含重复 PNG 载荷。

时间、tracemalloc 和 RSS 使用独立进程；各场景交替 AB/BA。时间在一次预热后测五次，输入准备、完整输出序列化与摘要校验位于计时之外。物化微测每次先创建新的独占 MiddleJson，该准备成本不计入物化阶段；postprocess 与完整 parse 使用真实公开入口。

B4 专用脚本复用 B0 冻结的 `public_output()`、运行身份检查及进程树 RSS 采样，没有修改 B0 驱动或新增输出忽略字段。同一工作负载的三个阶段、两版本及三种测量均得到相同完整输出摘要。

| 样本 | 阶段 | 中位耗时 B3→B4 | 时间比 | Python 分配峰值 B3→B4 | RSS 比 |
| --- | --- | ---: | ---: | ---: | ---: |
| text-10000 | 物化 | 52.17→16.11 ms | 0.3087 | 16.570→0.697 MiB | 0.9897 |
| text-10000 | postprocess | 172.21→129.25 ms | 0.7506 | 28.626→17.801 MiB | 0.8822 |
| text-10000 | 完整 HTML parse | 595.12→549.22 ms | 0.9229 | 34.246→23.422 MiB | 0.9103 |
| images-1000 | 物化 | 37.91→28.21 ms | 0.7443 | 2.322→0.807 MiB | 0.9998 |
| images-1000 | postprocess | 385.48→382.80 ms | 0.9930 | 4.056→2.883 MiB | 0.9843 |
| images-1000 | 完整 HTML parse | 501.23→487.99 ms | 0.9736 | 4.700→3.546 MiB | 0.9994 |

分配数值是预热后单次入口新增 Python 分配的峰值，不是整个进程 RSS。RSS 复用 50 ms 间隔的同一时刻进程树合计口径；短物化阶段可能少于采样间隔，绝对 RSS 也受预热后分配器保留内存影响，不能将微测分配下降直接当成等比例 RSS 下降。

六个场景均没有触发热运行或 RSS 的 5% 复测门槛。首次调用单列如下，均未出现超过门槛的退化：

| 样本 | 物化首次 B3→B4 | postprocess 首次 B3→B4 | 完整 parse 首次 B3→B4 |
| --- | ---: | ---: | ---: |
| text-10000 | 50.33→7.93 ms | 172.39→121.50 ms | 622.16→568.56 ms |
| images-1000 | 40.68→27.20 ms | 395.00→380.08 ms | 513.95→500.87 ms |

微测收益与全文收益分别报告。图像密集样本的 postprocess 耗时变化很小，本批最明确的效果是消除复制以及降低分配。证据为 `workloads.json`、`materialization-report.json`、`materialization-commands.json` 与各场景原始输出。

## 5. 真实 PDF 端到端表现

采用未修改的 B0 配对驱动，每版本预热后五次，逐文档交替版本顺序，独立进程测 RSS，测量期间没有并行构建、测试或渲染。

| 样本 | 页数 | B3 中位时间 | B4 中位时间 | 时间比 | RSS 比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| nougat | 17 | 1.9075 s | 1.8061 s | 0.9469 | 0.9792 |
| research-report | 5 | 0.5969 s | 0.5878 s | 0.9847 | 0.9918 |
| 2022.emnlp-main.614 | 18 | 2.2988 s | 2.3271 s | 1.0123 | 0.9919 |
| guide | 61 | 5.0186 s | 5.0011 s | 0.9965 | 1.0512 |
| 2407.00079v4_origi-10 | 1 | 0.06850 s | 0.06867 s | 1.0025 | 1.0019 |

五个样本初测中位耗时合计 **9.8903→9.7908 秒**，比率 **0.9899**；逐样本时间比的几何均值为 **0.9883**。EMNLP 与单页样本略慢，均低于 5% 门槛，不宣称每份 PDF 都提速。

guide 的初测 RSS 比率 **1.0512** 触发反序复测，复测为 **0.9600**，未持续退化；复测耗时 **5.0004→4.9933 秒**，比率 **0.9986**，完整输出仍一致。初始记录及重复测量均保留，没有用复测覆盖原始值。

首次调用（新进程，不声称清空操作系统文件缓存）分别为：

| 样本 | B3 首次 | B4 首次 |
| --- | ---: | ---: |
| nougat | 2.6311 s | 2.3498 s |
| research-report | 0.9701 s | 0.9594 s |
| 2022.emnlp-main.614 | 2.5812 s | 2.6788 s |
| guide | 5.4078 s | 5.3648 s |
| 2407.00079v4_origi-10 | 0.29496 s | 0.24830 s |

首次调用没有超过 5% 的退化。证据为 `pdf-performance/report.json`、`pdf-performance-summary.json` 与 `pdf-performance-command.json`。

## 6. 回归、语料与视觉

| 测试组 | 范围 | 结果 |
| --- | --- | --- |
| 主聚焦组 | 新所有权回归、document envelope、postprocess pages、render 所有权、公共 pipeline/export、图片命名与 HTML、完整离线链路 | 101 passed |
| 相关接口组 | middle export、metadata、PDF 输出文字、多格式矩阵、架构边界 | 109 passed |

新增的 10 项回归覆盖嵌套可变对象、keep_model_json、AnalysisResult/直接 ModelJson、原页号与损坏页补位、重复调用、素材冲突、不可变字节共享、晚发错误及公开接口失败隔离。既有回调、历史图片路径、重复素材、嵌套列表和 metadata/extensions 合同均通过。

PDF 沿用 B3 的 38 份固定输入：37 份共 443 页完整原生解析输出相同；另外 8 页扫描件保持相同 `pdf_ocr_unsupported` 错误代码和信息。追加 8 份真实 DOCX/PPTX/XLSX/RTF/ODT/EPUB/MHTML/OFD 文件及两份 HTML 压力输入，共 10 份、36 页，也全部相同。所有比较覆盖完整 ModelJson、MiddleJson、素材路径及字节摘要、诊断，执行前核对输入 SHA256。

视觉核验选取 nougat 第 2/7 页、研究报告第 2/5 页、EMNLP 第 4/16 页。六页原文与候选标框已人工查看，参考/候选标框的尺寸、模式和像素摘要全等。相同语义页面子集导出为 6 页 ORIGINAL、10 页 REFLOW，全部人工查看；三组导出分别核对 31、21、23 个文件，PDF、HTML、旁文件和诊断全等，页面尺寸、提取文字及链接信息也全等。

素材从保存的原始 ModelJson 重新物化后，先逐项核对 MiddleJson 和素材摘要，再参与渲染。未发现本批引入的图片、裁剪、表格或成员归属差异；既有字体、分页、表格样式、缩放与小字号诊断不变。选页子集的 REFLOW 接续不代表完整文档分页。

跨格式回放脚本初版漏建输出子目录，在首份结果写盘时失败。修复的是辅助脚本目录创建；产品代码和输出比较规则没有改变。初版脚本、错误日志及目录保留为 `replay_formats-initial.py`、`multi-format-reference-initial.log`、`multi-format-initial/`。

## 7. 身份与交付边界

证据根目录：`/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B04`。

| 项目 | 值 |
| --- | --- |
| 基础提交 | `b2ef8d025aed2b1041da6ec6e766e9d317f716ce` |
| 参考 | `B03/candidate-source`，已验收 B3 |
| 候选 | `B04/candidate-source`，仅叠加 api 与图片物化文件 |
| 参考源码/资源 SHA256 | `75c65e3636783ec78f449f060587baece85ff33be350cdcb4bcf92e19bdf2f23` |
| 候选源码/资源 SHA256 | `e3892d613d817f88f594efb8ccb980a1d7ec741e838d1a2c5a8e30adae57ba69` |
| 生产实现补丁 SHA256 | `94ca8724815d0a9bf9d560ceb3eda8f166f76fa21f26ea480456b94f237b42d1` |
| 两版本冻结扩展 SHA256 | `b75a91e149cc1c7d6101ee7a186b3aa1213499403a5a9a53ef7a5edb976c4c9f`，协议 36 |
| 工作区 pytest 扩展 SHA256 | `e1ccec4cf60f9d056d5d39edf64480c638b84d5e83cfd6b36e113bf1dd77437f`，协议 36 |
| B0 驱动清单中的摘要 | `9008a9b0495f840a766f36087cf4430408ba4e0ed88d6944c94ea5f2495a325e` |

运行环境为 CPython 3.14.4、macOS arm64、pypdfium2 5.13.0、pypdf 6.18.1；完整依赖与导入路径见 `identity.json`、`workspace-test-identity.json`。工作区实际导入位于对应的 `/Volumes/2TB/projects/20240809magic_pdf/docvortex`。保留工作区与冻结对照的不同扩展身份，Rust 源码逐文件相同；本批没有新增原生构建或 wheel 验证。

`initial-state.json`、`implementation.patch`、`change-manifest.json`、`change-snapshot/` 固定代码边界；`acceptance.json` 汇总验收。新增函数和测试辅助函数均有中文说明，Ruff 和差异检查通过。B0–B3 代码、冻结基准驱动及四个原有 recheck 文件保持不变。

本批完成本机阶段验收，不代替 B8 的累计性能、跨平台发行物及下游集成验证。**未提交、未推送、未合并、未发布；下一批为 B5。**
