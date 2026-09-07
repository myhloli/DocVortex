# JSON 外层协议 2.0 迁移验证

日期：2026-09-07。实现基于 DocVortex `30a24af` 与 MinerU `dc0944984` 的本地工作区。
本次未提交或发布软件包。协议和 Python 迁移说明见 [JSON_PROTOCOL.md](../JSON_PROTOCOL.md)。

## 实现结果

- Model/Middle 使用同一套带身份的 2.0 协议，格式与真实生产者进入必需的 `metadata`。
- Python 顶层旧字段、MinerU 旧协议封装和历史块转换被移除；产品扩展仅记录实际 tier 和 txt/ocr。
- 两种 Pydantic JSON Schema 模式均保留完整字段，读取验证身份与版本；来源和未知应用扩展无损。
- PDF ParseResult 继续省略图片字段，其他素材行为不变；同名应用扩展字段不参与图片省略。
- HTTP、ZIP、Gradio、Structured Content 和 Doclib 读写使用新结构。
- 旧缓存不命中、不贡献可用范围或默认读取档位；读取过期页提示重新解析。
- 压缩保留最新重复页，仅合并来源、扩展及整本标识相同的新批次；布尔和数值扩展不会混同。

## 测试结果

环境：本地 Magic-PDF `.venv1`，Python 3.13.5，DocVortex editable 安装已更新为 0.2.0。

| 检查 | 结果 |
| --- | --- |
| DocVortex 完整 pytest | 2682 passed，1 skipped |
| MinerU 完整 pytest | 1792 passed，3 skipped，3 个既有失败 |
| Gradio / HTTP / ZIP / 缓存集成专项 | 280 passed |
| 校验与序列化模式的 Schema 专项 | 26 passed |
| 新源码 Ruff | 两仓库所有修改的生产文件通过；DocVortex 全部 src 通过 |
| git diff --check | 两仓库通过 |

MinerU 的三个完整测试失败均使用 HEAD 原始测试文件复现；依赖测试同时使用
HEAD 原始 `pyproject.toml`，而本次只调整了 DocVortex 依赖约束：

1. `test_basic_extra_includes_preflight_runtime_dependencies`：现有 optional-dependencies 不含 basic。
2. `test_standard_is_the_highest_model_runtime_extra`：现有 test extra 不含 mineru[standard]。
3. `test_pp_formulanet_fix_latex_uses_shared_mathring_repair`：现有 UniMERNetDecode 不含 fix_latex。

上述无关依赖配置和公式算法未修改。MinerU 的既有 `test_doclib_cache_semantics.py`
有 13 项 Ruff 诊断，已核对与 HEAD 的诊断代码及内容一致；本次未扩大修改这些既有问题。

## 冻结语义和真实 PDF

迁移前后分别执行两边公开解析路径，冻结 6 份文档、12 组结果、共 28 个解析页：

- 文本 PDF：`tests/unittest/pdfs/test.pdf`。
- 含图表的 PDF：`tests/unittest/pdfs/flash_table_annotations_synthetic.pdf`。
- 真实跨页表格：`tests/unittest/pdfs/native_pdf_tables/manufacturing_facilities_cross_page_table.pdf`。
- 固定输入 HTML、CSV、DOCX，包含中文正文、链接及表格。

每个引擎独立对照自己的迁移前结果；Model 页面、Middle 页面树（包含几何与素材字段）、
Markdown、HTML、Structured Content 页面及 AssetStore 摘要全部一致。
PDF 基线使用 MinerU flash/txt，禁用可选 LLM 增强，以隔离协议迁移。
OCR/VLM 路由与产品记录另由现有路由、真实 HTTP 模拟服务及集成测试覆盖。

真实跨页表格在两边重新渲染，共 4 个输出 PDF 页面；经 Poppler 转为 PNG 后，
迁移前后文件及像素完全一致，并已进行页面视觉检查。
结果摘要见 [json-envelope-v2-baselines.json](json-envelope-v2-baselines.json)。

## 构建及证据

DocVortex wheel 与 sdist 已成功构建为 0.2.0，未发布。
已核对 wheel 中的 schema.py 与最终源码完全一致；仓库中的两份 JSON Schema
与当前类型的生成结果相等。

本地完整证据位于 `/tmp/docvortex-envelope-v2/`：

- `before/`、`final/`：完整语义基线和 PDF/PNG。
- `suite-dv-verified.log`、`suite-mu-verified.log`：最终完整测试输出。
- `preexisting-mu.log`、`preexisting-mfr.log`：HEAD 原始失败复现。
- `preexisting-lint.json`：原始与当前测试文件的相同 Ruff 诊断。
- `dist/`：构建产物；`build-verified.log`：构建记录。

旧 JSON、旧结果包和历史缓存需要从源文件重新生成。自建 MinerU 服务与客户端应同步升级；
协议迁移不会自动删除或改写用户已经保存的历史文件。
