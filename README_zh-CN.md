<div align="center">

<img src="https://gcore.jsdelivr.net/gh/myhloli/DocVortex@main/docs/images/docvortex-logo.jpg" alt="DocVortex 标志" width="200">

# DocVortex

**原生解析文档，一份结构，多种输出。**

[![PyPI](https://img.shields.io/pypi/v/docvortex?color=008cff)](https://pypi.org/project/docvortex/)
[![Python](https://img.shields.io/pypi/pyversions/docvortex)](https://pypi.org/project/docvortex/)
[![Downloads](https://static.pepy.tech/badge/docvortex)](https://pepy.tech/project/docvortex)
[![Monthly Downloads](https://static.pepy.tech/badge/docvortex/month)](https://pepy.tech/project/docvortex)
[![CI](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml/badge.svg)](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md)

[English](https://github.com/myhloli/DocVortex/blob/main/README.md) · **简体中文**

[性能提升](#pdf-性能) · [快速开始](#快速开始) · [格式支持](#格式支持) · [文档导航](#文档导航)

</div>

## 让文档流向更多可能

DocVortex 是一个独立的 Python 文档解析与转换引擎。
它读取文档中的原生文字与结构，整理为统一的中间表示，
再按工作流需要导出为不同格式。

- **多格式输入** — 支持文本与扫描 PDF、Office、OpenDocument、EPUB、HTML、OFD、CSV 和 TSV。
- **一次解析，多种导出** — 同一份结果可生成 Markdown、HTML、LaTeX、DOCX、EPUB、PDF 和结构化 JSON。
- **结果可携带** — 将文档结构与图像素材保存为 Bundle，离开源文件也能继续导出。
- **API 可组合** — 直接调用完整流程，或分别接入分析、后处理和渲染阶段。

原生解析无需 OCR 或 VLM 推理服务。您可通过 CLI 或 Python SDK 便捷的使用 DocVortex。

扫描 PDF 使用本地 CPU OCR。首次 OCR 解析会自动从 Hugging Face 下载 PP-DocLayoutV2 与 PP-OCRv6 Tiny Det / Small Rec，失败时切换 ModelScope，缓存到 `~/.docvortex/model/`。六个模型及配置文件合计约 239 MB；缓存完整后可离线解析。

![DocVortex 转换流程：原生文档经过统一中间表示，导出为 Markdown、HTML、LaTeX、DOCX、EPUB、PDF 或结构化 JSON。](https://gcore.jsdelivr.net/gh/myhloli/DocVortex@main/docs/images/docvortex-overview.jpg)

## PDF 性能

**正式 0.5.10 使用 Rust 加速，在所测语料上，公开解析速度达到 0.4.25 的 2.93 倍。**

| 处理链路 | 0.4.25 python | 0.5.10 rust | 加速倍数 | 耗时降低 | 峰值 RSS 降低 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DocVortex 公开 `parse()` | 42.36 s | 14.48 s | **2.93×** | **65.82%** | **31.17%** |
| MinerU Flash | 44.50 s | 14.44 s | **3.08×** | **67.55%** | **29.84%** |
| MinerU 共享 PDF 处理 | 76.28 s | 12.85 s | **5.94×** | **83.16%** | **35.65%** |

同机热运行对比，覆盖 32 份 PDF／299 页（Flash 为 31 份）。0.4.25 沿用既有测量结果；0.5.10 逐文档预热一次、计时五次，进行三轮平衡配对，取三轮中位数的中位数后求和。RSS 降幅为逐文档进程树峰值 RSS 降幅的中位数。MinerU 数据包含双仓优化收益；共享路径不含模型计算，剔除极端密集表格样本后为 **2.87×**。

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
根命令参数 `--log-level` 控制 loguru 日志等级，默认为 `info`，必须放在子命令之前；
也可以使用 `DOCVORTEX_LOG_LEVEL=warning` 配置。

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

### 原生输入 · 20 种格式

| 文档类别 | 文件后缀 |
| --- | --- |
| 含原生文字或扫描页面的 PDF | `.pdf` |
| Word 与富文本 | `.doc`, `.docx`, `.rtf` |
| 演示文稿 | `.ppt`, `.pptx` |
| 电子表格 | `.xls`, `.xlsx`, `.csv`, `.tsv` |
| OpenDocument | `.odt`, `.ods`, `.odp` |
| 电子书 | `.epub` |
| 网页文档 | `.html`, `.htm`, `.shtml` |
| 网页归档 | `.mhtml`, `.mht` |
| 开放版式文档 | `.ofd` |

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

- **PDF 解析：** 默认 `parse_mode="auto"` 对选定页面分类，自动选择原生文字或本地 CPU OCR。`parse_mode="txt"` 强制原生解析，不分类、不下载模型；`parse_mode="ocr"` 强制 OCR。CLI 使用 `--parse-mode auto|txt|ocr`。显式模式仅适用于 PDF。
- **PDF OCR：** layout 提供块区域与阅读顺序，文本块使用 Tiny Det / Small Rec。独立公式、图片、图表和印章保留截图，行内公式随文本做普通 OCR。表格输出空间投影文字及截图，不识别单元格结构。首版不使用页面方向分类或去畸变模型；所选文档整体采用一种解析后端。

混合 PDF 若被分类为 `txt`，但包含扫描页，可使用 `parse_mode="ocr"`（CLI：`--parse-mode ocr`）对所选文档整体做 OCR。
- **PDF 分类：** `docvortex classify report.pdf` 返回 `txt` 或 `ocr`，不推理、不联网。元数据提取及底层 `PdfModel.predict()` 原生文字入口也保持离线。详见[模型来源](licenses/models/README.md)。
- **PDF 导出：** 具有页面几何的 PDF 来源默认按原始块布局还原，正文可选择，表格优先使用可选择文字的 HTML 结构表，图表保留区域图；其他来源和旧结果继续语义重排。可通过 `--pdf-layout original|reflow` 显式选择，字体、换行与绘图指令不保证无损复现。详见 [PDF 布局选项](docs/USAGE.md#pdf-output-layout)。

## 文档导航

| 指南 | 内容 |
| --- | --- |
| [进阶使用](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md) | 分阶段 API、PDF 选页、分类、图像与 Bundle |
| [Agent skill](skills/docvortex/SKILL.md) | 面向 agent 的 CLI 与 Python SDK 工作流；复制整个 `skills/docvortex` 目录即可复用 |
| [示例](https://github.com/myhloli/DocVortex/blob/main/demo/README.md) | 本地 PDF、Office 样本与可运行示例 |
| [元数据](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md) | 源文档属性与各格式支持范围 |
| [JSON 协议](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md) | 文档结构、扩展字段与协议迁移 |
| [HTML 协议](https://github.com/myhloli/DocVortex/blob/main/docs/HTML_PROTOCOL.md) | 语义标记与往返转换 |
| [公共 SDK 与迁移](https://github.com/myhloli/DocVortex/blob/main/docs/sdk-0.4.md) | 公开集成边界与 0.4 升级说明 |
| [渲染职责](https://github.com/myhloli/DocVortex/blob/main/docs/RENDER_OWNERSHIP.md) | DocVortex 导出与 MinerU 专属渲染器 |

## 开发

请先阅读[仓库架构](docs/ARCHITECTURE.md)，了解目录职责、Python/Rust 边界，以及纯 Python 和原生扩展的开发与验证命令。

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
