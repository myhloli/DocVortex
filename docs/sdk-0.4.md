# DocVortex 0.4 公共 SDK 与宿主迁移

本文记录 0.4 系列的公共模块边界。MinerU 的共享图片导出需要 `docvortex>=0.4.19,<1`，文档协议、解析路由、模型回退与渲染语义保持不变。

## 公开入口

| 原入口或职责 | 新入口 |
| --- | --- |
| foundation 几何及 PDF 通用坐标原语 | `docvortex.geometry` |
| foundation 文本与链接规则 | `docvortex.content.text`、`docvortex.content.links` |
| 图像统计、素材编码与路径校验 | `docvortex.assets` |
| PDFDocument、PDFPage、字符和几何契约 | `docvortex.document.pdf` |
| HTML 来源上下文 | `docvortex.document.contracts.HtmlSourceContext` |
| 原生文字证据、区域表格及 OCR 文本投影 | `docvortex.analyzers.pdf` |
| 模型文本、坐标自动判断与虚拟 OCR token | MinerU PDF 分析层 |
| xhigh layout 容器补全 | MinerU PDF 分析层 |
| 推理平台支持判断 | MinerU model/runtime |

`docvortex.public_api.PUBLIC_API` 是静态模块及符号清单。新增跨库依赖必须同时更新清单、文档和契约测试；不能通过基础实现、私有模块或动态别名绕过边界。

### 0.5.9 公开清单补全

0.5.9 将已有的显式导出补入 `PUBLIC_API`，不改变函数签名、结果协议或默认后端选择。

| 职责 | 支持的入口 |
| --- | --- |
| 顶层便捷入口 | `docvortex.postprocess_document`、`docvortex.render_artifact`、`docvortex.__version__` |
| 结果与诊断类型 | `docvortex.result` 的 `Diagnostic`、`MetadataResult`、`AnalysisResult`、`DocumentResult`、`RenderArtifact`、`ExportResult`；除 `Diagnostic` 外也可从顶层导入 |
| 统一渲染门面 | `docvortex.render` 的格式、选项、输出类型、素材回调和各格式渲染函数；与 `docvortex.render.contracts` 共用类型对象 |
| 行内内容变换 | `docvortex.content.inline` 的规范化、文本映射、替换、切片与去首尾空白函数 |
| Span 构造与变换 | `docvortex.content.spans` 的代码、超链接、文本构造，以及扩展、规范化与切片函数 |
| 子块遍历与列表标记 | `docvortex.content.tree.iter_child_blocks`；`docvortex.render.fragments` 的 `ListItem`、`ListItemKind`、`OrderedListStyle` |

顶层 `render_artifact` 接收 `MiddleJson` 和可选素材并返回 `RenderArtifact`；`docvortex.render.render`
接收 `MiddleJson` 并返回 `RenderOutput`（字符串、字节或字典）。两者的职责与签名保持原样，调用方应选择对应层级。
契约测试同时检查清单符号可导入、显式 `__all__` 导出完整登记，以及门面类型身份一致。
现有兼容的 MinerU 无需修改调用代码；PDF 原生表格、标题与阅读顺序修复由 DocVortex 内部完成。

### 0.5.10 Rust 优化与 MinerU 兼容性

0.5.10 的优化均位于内部内核与适配层。公开的 41 个模块、321 个符号及类方法签名与 0.5.9 一致，
`PUBLIC_API`、ModelJson／MiddleJson、结果类型、素材格式与 `auto|python|rust` 选择方式不变。
Python／Rust 私有扩展协议由 30 升至 31；安装包须使用同一版本的 Python 源码和扩展，不应单独替换旧扩展。

现有兼容的 MinerU 无需修改调用代码或依赖范围。原生文字、表格和图片处理自动使用内部优化，
`PDFRenderSession` 的签名和资源关闭约定保持原样；macOS 页图使用共享内存传递独立像素，其他平台保持原有文件路径。
本地验证覆盖公开契约、当前 MinerU 的导入边界、文档协议、路由、表格、页面快照与渲染调度；
性能与输出对照见 [0.5.10 优化记录](rust-rule-acceleration-0.5.10.md)。

## 结果包素材导出（0.4.3）

跨库调用方可通过 `docvortex.export.materialize_middle(middle_json, assets=None)` 获取图片外置后的文档副本与 `AssetStore`，再通过 `validate_materialized_assets(document, assets)` 验证所有图片引用。直接图片和视觉 HTML 内嵌图片使用安全相对路径，原始图片字节及布局方向扩展保持不变；代码字面量不作为 HTML 素材处理。

### 0.4.19 图片命名与宿主回调

新物化图片统一沿用 MinerU Gradio 的命名：直接载荷为
`images/page_{page_idx}_{owner.type}_{owner.index}.{ext}`，其中 owner 是所属
image/table/chart 父块，独立公式使用 equation。视觉正文内的图片分别使用
`image_{index}_{ordinal}`、`table_image_{index}_{ordinal}`、`chart_image_{index}_{ordinal}`。
页号为从 0 开始的原始页索引；序号按每个正文的所有 `<img src=...>` 从 1 计数，外链也占序号。
扩展名转小写，缺省 jpg；图片字节不重新编码。同名同字节复用，同名异字节追加
`_duplicate_n`，最多检查 10000 个候选名称；不同语义名称不因字节相同而合并。

`materialize_middle` 新增两个可选的仅限关键字参数：

- `image_resolver: Callable[[ImagePayloadBlock, int], tuple[bytes, str] | None]`：接收副本中的载荷块和原始页号，返回字节及扩展名；返回 None 时保留载荷，不再触发默认解析。
- `asset_resolver: Callable[[str], bytes]`：接收经过实体/URL 解码及安全校验的 HTML 图片相对路径，返回字节；扩展名从引用路径取得。仅在明确传入回调时读取宿主资源，回调负责自身根目录限制。

不提供回调时保持内嵌图片的严格解析，不自动读文件或裁 PDF；已有相对路径保持原样。
输入 `assets` 保留为独立副本并参与冲突检查，重复物化不改变路径或字节。
新命名同步更新直接引用和 HTML src，保留 HTML 的属性、引号及其他正文。

此变更会改变新生成的 CLI/API ZIP 及 DocVortex 导出图片名称，消费者应读取文档实际引用，
不再自行拼接旧的 `*_body_*` 名称。历史结果包仍按已有引用读取，不自动迁移、不生成旧名别名；
MiddleJson 仍为 2.0，文件写出层的 overwrite 语义不变。

调用方负责将该副本及素材写入自己的结果包，不应先省略图片再从源 PDF 重裁。缺失引用、未物化网络图片或同名素材冲突会显式失败。物化不修改输入对象、不读取源 PDF、不下载网络资源；已有外部素材须通过 `AssetStore` 显式提供。

## 行为与资源所有权

`prepare_text_evidence` 返回具名的 `PDFTextEvidence`，`apply_text_evidence` 统一完成链接、样式、脚本与 InlineSpan 物化。MinerU 仍决定调用时机、公式排除区域、超大字符页和 OCR 回退。

`prepare_table_page` 仅在存在候选表格时调用；同页复用其物化数据。`recover_table_region` 返回最终 HTML、来源、置信度和诊断，`None` 表示没有可接受结果。结构恢复异常以 `PDFTableRecoveryError` 保留原始原因，交给宿主回退；HTML 物化异常继续传播，避免改变旧异常边界。页面几何可以复用，证据不持有 PDFium 裸句柄。

从 0.4.15 起，`PDFPage.get_vector_geometry()` 一次遍历返回公开的 `PDFPageVectorGeometry`，其中
`drawing_lines` 与 `path_infos` 是物化元组。`prepare_table_page` 和 `prepare_text_evidence` 都接受可选的
`vector_geometry=`，可与 `geometry=` 一起复用。调用方按页、按处理窗口持有并释放这些数据；`PDFPage`
本身不缓存。文字证据构造只读访问源字符，旋转和组行累加使用独立的局部对象。

通用 `convert_bbox` 显式声明 `unit`、`pixel` 或 `point`；`page_size` 使用 point，`render_scale` 是每 point 的像素数。模型 bbox 的自动解释与三位小数规则保留在 MinerU。共享 PDFium 锁、进程池和资源关闭职责仍归 DocVortex。

## 升级与回退

本轮直接删除被替代的旧门面及重导出，不提供过渡版本或旧 pickle 路径兼容。JSON、Bundle、HTTP API 和输出格式保持既有契约。

配套升级两个 wheel；旧版 MinerU 必须显式约束 `docvortex<0.4.0`。旧版依赖上限 `<1` 无法阻止解析器选择 0.4，因此不要只升级 DocVortex。回退时同时恢复旧版 MinerU 与其兼容的 DocVortex。

## 验证

`tools/validate_docvortex_boundary.py` 可对指定两仓库源码或当前安装包捕获真实 PDF 的中间协议与全部渲染。记录实际导入路径、样本 SHA256 和页数；比较时只允许忽略 DocVortex producer 版本、DOCX/EPUB 容器时间字段，以及经独立复算确认由 producer 版本派生的 EPUB 标识。

MinerU 的 `DocVortex boundary integration` 手动工作流接受未发布的 0.4 wheel URL，以及可选的 mineru-vl-utils 配套 wheel URL，在三种操作系统及 Python 3.10、3.13、3.14 上验证边界、协议和固定推理结果。DocVortex 自身 CI 验证独立安装与基础算法。配置工作流不代表已经通过远端 CI。
