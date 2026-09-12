<div align="center">

<img src="https://raw.githubusercontent.com/myhloli/DocVortex/main/docs/images/docvortex-logo.jpg" alt="DocVortex 标志" width="200">

# DocVortex

**原生解析文档，一份结构，多种输出。**

[![PyPI](https://img.shields.io/pypi/v/docvortex?color=008cff)](https://pypi.org/project/docvortex/)
[![Python](https://img.shields.io/pypi/pyversions/docvortex)](https://pypi.org/project/docvortex/)
[![CI](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml/badge.svg)](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md)

[English](https://github.com/myhloli/DocVortex/blob/main/README.md) · **简体中文**

[快速开始](#快速开始) · [格式支持](#格式支持) · [文档导航](#文档导航)

</div>

## 让文档流向更多可能

DocVortex 是一个独立的 Python 文档解析与转换引擎。
它读取文档中的原生文字与结构，整理为统一的中间表示，
再按工作流需要导出为不同格式。

- **多格式输入** — 支持文本 PDF、Office、OpenDocument、EPUB、HTML、OFD 和 CSV。
- **一次解析，多种导出** — 同一份结果可生成 Markdown、HTML、LaTeX、DOCX、EPUB、PDF 和结构化 JSON。
- **结果可携带** — 将文档结构与图像素材保存为 Bundle，离开源文件也能继续导出。
- **API 可组合** — 直接调用完整流程，或分别接入分析、后处理和渲染阶段。

原生解析无需 OCR 或 VLM 推理服务。通过 CLI 或 Python SDK
即可独立使用 DocVortex，无需安装 MinerU。

![DocVortex 转换流程：原生文档经过统一中间表示，导出为 Markdown、HTML、LaTeX、DOCX、EPUB、PDF 或结构化 JSON。](https://raw.githubusercontent.com/myhloli/DocVortex/main/docs/images/docvortex-overview.jpg)

## 快速开始

需要 **Python 3.10–3.14**。

### 安装

```bash
pip install docvortex
```

### 命令行

将文本 PDF 转换为 Markdown：

```bash
docvortex convert report.pdf --format markdown --output output/report.md
```

将 `report.pdf` 替换为任意受支持格式的本地文件。
通过 `--format` 选择输出格式，运行 `docvortex convert --help` 查看选项。

### Python

解析一次，导出两种格式：

```python
import docvortex

result = docvortex.parse("report.pdf")
result.export("output/report.md", output_format="markdown")
result.export("output/report.docx", output_format="docx")
```

解析结果持有文档结构与素材，后续导出无需重新打开或解析源文件。
默认保护已有输出文件；如需替换，在 Python 中设置 `overwrite=True`，
或在 CLI 中添加 `--overwrite`。

## 格式支持

### 原生输入 · 15 种格式

| 文档类别 | 格式 |
| --- | --- |
| 含原生文字的 PDF | PDF |
| Word 与富文本 | DOC, DOCX, RTF |
| 演示文稿 | PPT, PPTX |
| 电子表格 | XLS, XLSX, CSV |
| OpenDocument | ODT, ODS, ODP |
| 电子书与网页文档 | EPUB, HTML |
| 开放版式文档 | OFD |

### 输出 · 7 种格式

| 输出 | `--format` / `output_format` |
| --- | --- |
| Markdown | `markdown` |
| HTML | `html` |
| LaTeX | `latex` |
| Word 文档 | `docx` |
| EPUB 电子书 | `epub` |
| PDF | `pdf` |
| 结构化 JSON | `structured_content` |

PPT/PPTX、XLS/XLSX 仅支持输入。结构化 JSON 是一种导出格式；
分析结果与中间表示的结构另见
[文档 JSON 协议](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md)。

## 保存结果，随时导出

Bundle 将解析后的文档与图像素材打包，便于跨进程、跨机器复用。
需要新格式时，加载已有结果即可：

```python
import docvortex

result = docvortex.parse("report.pdf")
result.save_bundle("output/report.bundle")

restored = docvortex.load_bundle("output/report.bundle")
restored.export("output/report.epub", output_format="epub")
```

恢复后的结果无需依赖源文件。Bundle 内容、素材处理和覆盖规则见
[进阶使用指南](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md#portable-bundles)。

只需要标题、作者等源文档属性？
[`docvortex.extract_metadata()`](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md)
可以直接读取元数据，无需解析正文。

## 选择适合的处理方式

- **文本 PDF：** 原生解析利用文档已有的文字与结构。需要 OCR 的扫描页面应交给外部 OCR 或推理服务。
- **PDF 分类：** `docvortex classify report.pdf` 返回 `txt` 或 `ocr`。分类需要显式调用，不会启动推理；解析也不会自动切换后端。
- **PDF 导出：** 按文档语义重新排版，不保证无损复现原始页面布局与绘图指令。

## 文档导航

| 指南 | 内容 |
| --- | --- |
| [进阶使用](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md) | 分阶段 API、PDF 选页、分类、图像与 Bundle |
| [示例](https://github.com/myhloli/DocVortex/blob/main/demo/README.md) | 本地 PDF、Office 样本与可运行示例 |
| [元数据](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md) | 源文档属性与各格式支持范围 |
| [JSON 协议](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md) | 文档结构、扩展字段与协议迁移 |
| [HTML 协议](https://github.com/myhloli/DocVortex/blob/main/docs/HTML_PROTOCOL.md) | 语义标记与往返转换 |
| [公共 SDK 与迁移](https://github.com/myhloli/DocVortex/blob/main/docs/sdk-0.4.md) | 公开集成边界与 0.4 升级说明 |
| [渲染职责](https://github.com/myhloli/DocVortex/blob/main/docs/RENDER_OWNERSHIP.md) | DocVortex 导出与 MinerU 专属渲染器 |

## 开发

在本地仓库目录中运行：

```bash
uv venv
uv pip install -e ".[test,dev]"
uv run --no-project python -m pytest -q
uv run --no-project ruff check src
uv run --no-project ruff format --check src
uv build
```

欢迎反馈问题和贡献代码。报告解析问题时，请在
[GitHub Issues](https://github.com/myhloli/DocVortex/issues)
中附上可复现的命令及可以公开分享的样本文档。

## 许可证

DocVortex 项目代码采用
[MIT 许可证](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md)。
