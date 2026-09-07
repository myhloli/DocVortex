# 测试与资源迁移验证（2026-09-07）

本次按实现职责从 MinerU 迁移原生解析、共享类型、确定性后处理、渲染与导出测试。生产代码和协议未改动。

## 结果与核验范围

- 涉及 75 个原测试文件，DocVortex 接收 1309 个原测试函数。
- 逐项记录 2338 个原参数化测试项的去向，其中包含原有全部 1,439 个 native/flash 测试项；没有丢失原用例或参数集。
- 原有 462 个 DocVortex 测试项全部保留。两个仓库确有不同责任时，一个原用例可对应两边的测试，例如配置传递、产品元数据与 Content List 分支。
- [结构化清单](test-migration.json)包含原提交、每个参数化用例去向、资源 SHA256 与实际测试结果；总数不用于抵消遗漏。
- 15 项 ODF 用例的旧默认 ID 含生成 ZIP 的时间戳，现改用 `odt/ods/odp`。参数表达式和构造 helper 函数体经 AST 比较完全一致，15 项定向重跑通过。清单保留原 ID 的 SHA256，避免嵌入重复的大段临时 ZIP 字节。

| 验证 | 通过 | 跳过 | 失败 |
|---|---:|---:|---:|
| MinerU 迁移前 | 3970 | 4 | 3 |
| DocVortex 迁移前 | 462 | 0 | 0 |
| MinerU 迁移后（本地单元测试） | 1797 | 3 | 3 |
| DocVortex 独立环境、移开本地 OFD 语料 | 2645 | 12 | 0 |
| 恢复本地 OFD 后的既有样例 | 11 | 0 | 0 |

DocVortex 的独立全量运行不安装 MinerU，使用 Python 3.13.5、PDFium 5.10.1。跳过项为既有的 11 个本地 OFD 参数场景与未安装 TeX 工具链的可选编译测试；OFD 恢复后单独全部通过。没有新增 skip/xfail 或修改 CI 排除项。现有跨平台/Python CI 自动收集迁入测试；本次本地验证不等于远程 CI 已运行。

最终显式定界符与测试依赖边界定向复测 13 项通过。80 个引擎侧、40 个宿主侧变更 Python 文件通过 Ruff 导入/错误检查与格式检查，两个仓库均通过 `git diff --check`；生产源码目录无差异。

MinerU 迁移前后的三个失败完全相同，本次没有调整断言或排除它们：

- `tests/unittest/test_doclib_app_startup.py::test_basic_extra_includes_preflight_runtime_dependencies`
- `tests/unittest/test_doclib_app_startup.py::test_standard_is_the_highest_model_runtime_extra`
- `tests/unittest/test_mfr_latex_utils.py::test_pp_formulanet_fix_latex_uses_shared_mathring_repair`

## 拆分与资源归属

- DocVortex 保留原生算法的完整输入、参数集及语义断言；调用改用中性 Schema、显式选项、独立解析/渲染入口。
- 原生阶段需要检查未外置图片时，测试辅助函数真实执行公开 `analyze` 与引擎确定性后处理，保留该阶段内联载荷。
- MinerU 保留 OCR/Hybrid/VLM、CLI/API/Doclib、模式路由、宿主协议、元数据、配置传递、旧宿主路径移除、LLM 增强及 Content List。
- 曾扫描不存在的 Flash 目录的架构守卫迁到真实引擎目录，要求目录存在且含 Python 文件；保留原规则，没有把空集合当作通过。
- 16 份 PDF、1 份 XOR、两个 EMF+ 样本及原生表格真值保持原 SHA256。已删除 MinerU 的整个 `tests/unittest/pdfs`；demo1/demo2 保留。
- 表格评测器、合成 PDF 生成器、OFD/表格语料说明与 benchmark README 随资源迁移。13 个仍被宿主用例使用的 helper 保留依赖闭包，两个不再使用的 helper 删除。
- 原有 33 组 Hybrid 快照及历史 producer 内容逐字节保留。新增 5 组快照包括 4 个 CJK 探针和旋转页的 33 行；再生比较一致，相关 64 项 Hybrid 回归通过。引擎另直接验证原 PDF 的身份、字体、页旋转和物理行。
- 31 份 OFD 本地文件逐份校验哈希后复制，源副本保留；目标目录明确忽略，不纳入 Git。现有 11 个样例场景未扩展。

## 运行与再生

在各自仓库根目录运行：

```sh
# DocVortex：独立环境安装并运行，迁入测试仅额外需要 markdown
uv venv .venv-tests
uv pip install --python .venv-tests/bin/python -e '.[test]' 'pypdfium2==5.10.1'
.venv-tests/bin/python -m pytest -q

# MinerU：保留产品集成套件；既有三个失败会继续报告
.venv1/bin/python -m pytest tests/unittest -o addopts= -q -m 'not remote and not full_stack'

# DocVortex：完整表格 manifest 功能评测，不混入性能计时
.venv-tests/bin/python tests/fixtures/evaluate_native_pdf_table_manifest.py --skip-performance-gate

# DocVortex：冻结原生成工具版本以逐字节再生合成 PDF
uv run --no-project --python .venv-tests/bin/python --with reportlab==4.4.4 --with pillow==11.3.0 python tests/fixtures/generate_synthetic_pdf_fixtures.py --check
```

独立环境的 ReportLab 5.0.1 可以通过解析/渲染回归，但其生成字节与冻结 PDF 不同。原脚本在原工具版本下以及上述独立再生命令均通过；未重写 PDF 或弱化字节比较。

在 MinerU 根目录，通过捕获脚本再生检查新增 Hybrid 快照：

```sh
.venv1/bin/python tests/fixtures/capture_hybrid_native_script_inputs.py --fixture tests/fixtures/hybrid_pdf_script_inputs.json --source-root /path/to/DocVortex --check
```

旧夹具仍使用原默认 `--fixture` 与 demo/pdfs 作为 source root，未改默认接口；历史来源信息不随本次迁移改写。

## 文件级去向

表中数量为测试函数数，不是参数化测试项数。“两边保留”仅用于引擎与宿主各自的契约断言，逐项对应关系见 JSON。

| MinerU 原文件 | DocVortex 函数 | MinerU 保留函数 |
|---|---:|---:|
| `test_docx_assets.py` | 13 | 0 |
| `test_docx_complex_fields.py` | 6 | 0 |
| `test_docx_math.py` | 11 | 0 |
| `test_docx_render.py` | 36 | 0 |
| `test_docx_special_char_tables.py` | 8 | 0 |
| `test_docx_table.py` | 12 | 0 |
| `test_emfplus_integration.py` | 3 | 0 |
| `test_epub_render.py` | 14 | 0 |
| `test_flash_csv.py` | 11 | 3 |
| `test_flash_docx_equationxml.py` | 13 | 1 |
| `test_flash_epub.py` | 31 | 12 |
| `test_flash_html.py` | 76 | 9 |
| `test_flash_image_mtef.py` | 11 | 0 |
| `test_flash_legacy_chart_data.py` | 6 | 0 |
| `test_flash_legacy_doc.py` | 11 | 2 |
| `test_flash_legacy_doc_mtef.py` | 7 | 1 |
| `test_flash_legacy_ppt.py` | 7 | 2 |
| `test_flash_legacy_xls.py` | 12 | 2 |
| `test_flash_legacy_xls_ppt_mtef.py` | 6 | 1 |
| `test_flash_markup_anchors.py` | 4 | 0 |
| `test_flash_model.py` | 2 | 1 |
| `test_flash_mtef_v5.py` | 18 | 0 |
| `test_flash_odf.py` | 50 | 8 |
| `test_flash_ofd.py` | 31 | 4 |
| `test_flash_office_image_mtef.py` | 18 | 1 |
| `test_flash_office_metafile.py` | 12 | 0 |
| `test_flash_office_mtef_v5.py` | 11 | 1 |
| `test_flash_office_package_normalizers.py` | 3 | 0 |
| `test_flash_ooxml_mtef.py` | 15 | 1 |
| `test_flash_pdf_auxiliary_text.py` | 49 | 0 |
| `test_flash_pdf_document_profile.py` | 10 | 0 |
| `test_flash_pdf_extractor.py` | 2 | 0 |
| `test_flash_pdf_formulas.py` | 35 | 0 |
| `test_flash_pdf_graphics.py` | 20 | 0 |
| `test_flash_pdf_index_blocks.py` | 6 | 0 |
| `test_flash_pdf_line_metadata.py` | 6 | 0 |
| `test_flash_pdf_native_text.py` | 16 | 0 |
| `test_flash_pdf_pipeline.py` | 18 | 0 |
| `test_flash_pdf_tables.py` | 44 | 0 |
| `test_flash_pdf_text_blocks.py` | 93 | 0 |
| `test_flash_pdf_titles.py` | 31 | 0 |
| `test_flash_pdf_visual_annotations.py` | 29 | 0 |
| `test_flash_rtf.py` | 28 | 5 |
| `test_flash_shared_helpers.py` | 10 | 0 |
| `test_flash_spreadsheet_projector.py` | 8 | 0 |
| `test_guess_suffix_ole2.py` | 4 | 0 |
| `test_html_render.py` | 30 | 0 |
| `test_html_security.py` | 24 | 0 |
| `test_latex_render.py` | 9 | 0 |
| `test_markdown_render.py` | 40 | 1 |
| `test_middle_json_block_models.py` | 30 | 0 |
| `test_middle_json_export.py` | 16 | 0 |
| `test_middle_json_validator.py` | 11 | 1 |
| `test_model_json.py` | 4 | 6 |
| `test_native_pdf_table.py` | 61 | 0 |
| `test_native_pdf_table_manifest.py` | 5 | 0 |
| `test_native_pdf_table_pipeline.py` | 1 | 11 |
| `test_pdf_render.py` | 14 | 0 |
| `test_pdf_render_diagnostics.py` | 9 | 0 |
| `test_pdf_text_styles.py` | 55 | 7 |
| `test_postprocess_page_blocks.py` | 5 | 0 |
| `test_postprocess_pages.py` | 18 | 0 |
| `test_postprocess_paragraphs.py` | 26 | 0 |
| `test_postprocess_visual.py` | 14 | 0 |
| `test_render_api.py` | 5 | 6 |
| `test_render_html_table.py` | 8 | 0 |
| `test_render_image_renderer.py` | 6 | 0 |
| `test_render_list_index_utils.py` | 9 | 0 |
| `test_structured_content_render.py` | 8 | 3 |
| `test_table_merge.py` | 15 | 0 |
| `test_table_merge_cell_merge.py` | 3 | 0 |
| `test_text_block_anchor.py` | 4 | 1 |
| `test_text_utils.py` | 2 | 0 |
| `test_backend_block_object_regressions.py` | 2 | 0 |
| `test_backend_architecture.py` | 8 | 11 |
