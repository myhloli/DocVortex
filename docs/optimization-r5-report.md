# R5 非PDF优化的CI与wheel交付验收

日期：2026-10-25。产品代码延续R4C，协议40不变。本报告记录验收机制与本机验证；远端结论以相同提交的GitHub Actions运行及证据目录`R5/task-state.json`为准，不将工作流配置或本机通过视为跨平台已通过。

## 1. 覆盖补齐

- `verify_native_abi.py`在无应用依赖的独立二进制进程中，新增非空电子表格区域发现/选择/子集筛选、RTF组扫描、OFD连接/拓扑及字形几何调用，同时检查结果和实际计数。
- 新`verify_nonpdf_wheel.py`拒绝环境外源码及editable安装，核对版本、协议、ABI3标签、RECORD和全部非原生源码资源。14份真实DOC/DOCX/PPT/PPTX/XLS/XLSX/RTF/ODT/ODS/ODP/HTML/MHTML/EPUB/OFD使用R4C已验收输出的固定页数与语义锚点，并绑定输入SHA256。
- 另生成CSV、TSV、ODS、RTF、OFD及嵌入字体OFD，共20份输入、覆盖16种格式。六份生成输入重复解析比较完整ModelJson/MiddleJson、素材及诊断；保存所有实际完整输出，要求四类原生阶段实际命中且无RTF扫描/表格预算失败。
- 生成ODS和嵌入字体OFD各实际写出七格式并回读bundle，共14次实际导出、2次bundle回读。跨平台真实输入使用固定语义锚点，完整算法等价由冻结Python回归另行把守；没有宣称所有平台的字体或导出字节普遍相同。
- 统一`--run-tests`入口包含N1–N5、R1–R4C及安装验证器的2363项分批回归，避免在多个工作流维护不同测试清单。

## 2. 矩阵接入

- 常规CI：三平台Python3.11/3.14的已安装wheel增加真实非PDF公开入口检查，原有全量测试继续执行。Linux3.12/3.13增加完整分批非PDF回归。
- 发行wheel：五个平台的cp311构建环境增加真实非PDF检查及分批回归；同一wheel安装到当前Python后再次检查，上传完整输出、导出和JUnit证据。
- Windows ARM64独立应用安装任务在Python3.11/3.14均执行相同非PDF验证，保留原有ABI、PDFium与ReportLab验收。
- `skip_ci`仍按原合同跳过测试；本次手动矩阵使用false。性能测试保持本机串行执行，不在共享runner上声称提速。
- 源分发清单补齐验收工具依赖；HTML/MHTML样本禁止Windows换行转换，保留输入摘要约束。

## 3. 本机验证

使用R4C已验收的同一ABI3 wheel，SHA256 `1958001433e353b3730a9c9564cb9151b36a59c927b3b294d69a5f86c383c6db`，产品源码/原生二进制不变。

- Python3.11.15、3.14.4各20份公开输入检查、14次实际导出、2次bundle回读和2363项分批回归通过，分别见`local-311-final`、`local-314-final`。
- 两端点均核对432个包文件；电子表格实际批次为`[10,3,6]`，RTF扫描`[63,149929,2486,0]`，OFD表格`[5,5,57,32,0]`，字形几何`[4,288,288]`。这是工作量证据，不是性能比较。
- 新ABI验证在两个Python端点通过；40项验证器/交接门禁回归通过；actionlint、Ruff及`git diff --check`通过，新增14个函数均有中文文档字符串。
- 初版验证器的上下文/导出接口导入错误在本地运行中发现并修正，失败报告保留于`local-314-01/02`，最终只采用`local-*-final`记录。
- 首轮远端发行wheel矩阵的Linux x86_64、Linux ARM64和macOS ARM64通过；Windows x86_64/ARM64的20份公开输入与导出通过，但同一源码身份回归使用默认CP1252读取UTF-8 lexer而失败（其余2362项通过）。已将读取改为显式UTF-8，保持生产代码不变，并按修正提交重新运行矩阵；原失败日志和产物保存在`initial-windows-failures`。

证据根目录：`/Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R5`。累计优化和CI改动分别提交；远端矩阵只构建和验收分支产物，不修改版本、合并main、创建标签或发布PyPI。
