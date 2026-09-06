# PDF 输出的全角英数归一化

DocGale 在 `PdfModel.predict()` 返回前统一 PDF 自然语言和表格单元格中的
全角英文字母与数字。`analyze()`、`parse()` 和 `convert()` 自动使用这一路径。
MinerU 在 PDF 回填、Span 构造和宿主元数据清理之后调用同一个实现，因此
Flash、Hybrid 和 OCR/VLM 的 PDF 输出使用相同规则。

```python
from docgale.content import normalize_pdf_model_text

pages = [[{"type": "text", "content": "ＡＢＣ１２３，。"}]]
normalize_pdf_model_text(pages)
assert pages[0][0]["content"] == "ABC123，。"
```

该函数原地修改 `model_list`，返回 `None`，可以重复调用。它不增加高层配置或
改变 ModelJson/MiddleJson 协议，也不把字符串转换为 Span。

- 只转换 U+FF10–FF19、U+FF21–FF3A、U+FF41–FF5A；保留全角标点、全角空格、
  圈号、兼容单位、罗马数字、组合字符等，不做整体 NFKC。
- 自然语言只改 TextSpan 和链接显示文字，不合并或重建 Span。链接目标、样式、
  行内公式和代码、代码块及算法载荷保持原样。
- 已有 `\(...\)`、`\[...\]` 公式范围保持原样，允许定界符跨样式或链接显示
  节点。未闭合公式保护到当前逻辑文字段末尾；不从普通文字外观猜测公式。
- 表格只改 `td/th` 可见文本，保留行列结构、属性值、合并关系、上下标和素材。
  `<eq>`、`<math>`、现有公式载体、代码和 SVG 等载荷不会被改写。公式定界符
  可以跨 HTML 文本节点，但保护状态不会延续到下一个单元格。
- HTML 数字字符实体也会被处理。有修改时使用现有 BeautifulSoup HTML 解析器
  序列化，标签拼写形式可能规范化；没有实际文字修改时，保留原 HTML 字符串。

清洗发生在布局、上下标、样式和链接匹配完成之后。`PDFDocument` 的原始
Unicode、字符索引、`source_indices`、字体及 loose/tight/origin 几何不变。
Layout 底图仍显示 PDF 原字形，半角文字出现在模型输出和后续语义渲染中。

Office、HTML、EPUB、OFD、CSV 等非 PDF 输入不会自动调用此函数。ModelJson
构造、加载、序列化、显式后处理和 renderer 也不会自动重写旧文本。旧 Doclib
缓存继续有效；升级后重启正在运行的服务，再通过现有 `--force` 重新解析以获取新输出。

清洗单测由 DocGale 维护。MinerU 保留委托时机、类型转换和 Flash/Hybrid/OCR/VLM
出口测试；源字符、布局、表格结构和字体策略的既有断言继续适用。
