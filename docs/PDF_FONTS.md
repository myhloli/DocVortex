# PDFium 的固定 CJK 字体

DocGale 在首次 PDF 访问时安装进程级 PDFium 字体提供器。有效嵌入字体继续
使用 PDF 中的字体；未嵌入字体的 CJK 替代请求统一使用发行包里的
Droid Sans Fallback Full。非 CJK 请求继续交给 PDFium 的默认提供器。
无需网络、系统字体安装或额外 Python 依赖。

```python
from docgale.document.pdf import PDFDocument, initialize_pdfium_runtime

# DocGale 自动完成此步骤。直接使用 pypdfium2 的宿主应在首次字体使用前调用。
runtime = initialize_pdfium_runtime()
print(runtime.font_policy, runtime.font_sha256)
with PDFDocument("paper.pdf") as document:
    classification = document.classify()
```

直接访问 pypdfium2 时仍需使用 `docgale.document.pdf.pdfium.pdfium_guard()`
序列化原生操作，包括打开/读取/渲染及关闭句柄。初始化本身不使 PDFium
支持并发调用。运行时不提供逐文档字体切换，也不会销毁并重新初始化 PDFium。
如果宿主已用过其他字体策略，应启动新进程后再提前初始化；原有字体缓存不能
通过这个接口追溯改写。多进程使用 spawn/forkserver，在每个 worker 中独立
初始化。DocGale 自带的渲染进程池已经完成接入及父进程退出监控。

`PdfiumRuntimeInfo` 包含 `pdfium_version`、`font_policy`、`font_name` 和
`font_sha256`。字体策略为 `droid-cjk-v1`。资源缺失、SHA256 不匹配和回调故障
抛出 `PdfiumFontError`，不会静默退回系统 CJK 字体。回调错误在原生调用退出后
报告；发生故障的进程不再继续处理 PDF。模块导入不读取字体文件，清理路径
不会触发初始化。

## 替代范围

CJK 字符集 GB2312、Big5、Shift-JIS、Hangul 优先识别，并补充明确的常见字体
族名、子集前缀和 GBK/GB2312/Big5 编码别名。明确的 Symbol 和其他语言字符集
不会被名称规则覆盖。无法识别的请求保留默认处理，并通过
`docgale.document.pdf.font_runtime` 的 DEBUG 日志记录有限数量的委托诊断。
该接口无法从一个没有任何文字或字符集信息的任意私有字体名推断其语言。

首版面向横排 CJK，包括页面旋转及旋转的横排文字。字体不解决错误 ToUnicode、
私有编码、OCR、生僻字超出字库覆盖或竖排排版优化问题。PDFium 的原始 Unicode、
字符索引以及 DocGale 的 `source_indices` 规则不变。缺字环境更换字体后，PDFium
自动生成的空格数量可能改变，旧缺字环境中被省略的 CJK 字符记录也会补齐；
后续数值索引会随之移动，不跨策略复用旧索引。
同一固定策略的三平台比较仍严格检查全部索引和 Unicode。不同字体可能同时影响
loose 和 tight bbox；原始 loose bbox 也不被承诺与旧系统字体完全相同。

此策略只控制源 PDF 的 PDFium 字体替代，不修改 HTML 的 CSS 字体，也不改变
语义重排版 PDF 的字体嵌入策略。运行时信息不进入 ModelJson/MiddleJson 协议，
而是记录在平台产物及性能报告中。MinerU 的既有 Doclib 结果仍可读取；需要新
策略结果时使用现有 `mineru parse ... --force` 重新解析。

## 资源与验证

字体原文件来自 MuPDF 仓库提交
`398b9126136fae6ffa78fb40bc768f2ebfdc4fa4` 的独立 Apache-2.0 字体资源，未裁剪、
未修改；不引入 MuPDF 代码或运行时。固定 SHA256 为
`8a4dea0899424438af25a6f1f6eb61e5d111d85367eece0a5d30170861ae6b2e`。
完整来源见 `THIRD_PARTY_NOTICES.md` 和包内 `resources/fonts/manifest.json`，
对应 NOTICE 随 wheel 与 sdist 分发。

`tools/capture_platform_layout.py` 同时记录实际字体数据 SHA256、原始字符几何、
ModelJson、MiddleJson、源页面、layout 标注及 Render HTML。
`tools/compare_font_platforms.py` 对固定 PDFium/font 的产物比较全部原始字符索引
和 Unicode；被 Droid 接管的字形使用 0.001 PDF point 几何容差，并精确检查
块类型、阅读顺序、文本及表格 HTML。保留默认处理的非 CJK 字体差异单独记录，
不将 PNG 字节一致作为条件。运行方式：

```bash
uv run --no-project python tools/capture_platform_layout.py --output platform-output
uv run --no-project python tools/compare_font_platforms.py macos-output linux-output windows-output --output comparison.json
```
