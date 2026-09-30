# 少线分组表格真实语料

`emnlp2022_grouped_tables.pdf` 是用户提供的 EMNLP 2022 论文
*Incorporating Relevance Feedback for Information-Seeking Retrieval using Few-Shot Document Re-Ranking*。
原文件名为 `2022.emnlp-main.614.pdf`，保留完整 18 页，未改动 PDF 内容。

SHA256：`b10fa41a07177c40a45fd292d7e8147f4905fc41466fa3b61fed25271e2b281b`。

真值位于 `tests/fixtures/native_pdf_grouped_tables.json`，坐标为页面左上原点的 PDF point，页码为物理页。
该真值保存恢复候选生成前的独立字符行证据；候选逐格内容、完整字符捕获、唯一来源归属均须匹配。
`text_rows` 保留原始字符行证据；`group_titles` 单独记录原图及修复前原生空间投影核对的标题词间空格。
新增跨列标题格复用原生投影补齐词间距，去掉空白后的字符必须与原格一致；普通数据格仍沿用已有重建规则。

| 物理页 | 区域 | 预期结构 |
|---|---|---|
| 7 | 表 3 | 36 × 6 |
| 8 | 表 4 | 18 × 6 |
| 15 | 表 6 左、右 | 各 46 × 6 |
| 17 | 表 7、8 | 36 × 6、18 × 6 |

各分组标题占完整一行，输出六列 `colspan`。表 4、8 的首条数据行保留空白首列。
本目录独立于既有 `native_pdf_tables` 清单，避免把未标注的论文其他表格自动纳入旧跨页评测。
