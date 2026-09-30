# 页面 Form 容器回归原件

两份原件来自 [DocVortex Issue #23](https://github.com/myhloli/DocVortex/issues/23)。保持附件字节不变；只用于离线解析回归。

| 本地文件 | 来源附件 | SHA-256 |
| --- | --- | --- |
| research-report.pdf | https://github.com/user-attachments/files/32798086/7-.pdf | db4ab117330975581f03733c9f328c597bff49d6fa7c33bd671d268f8e86cce7 |
| 2022.emnlp-main.614.pdf | https://github.com/user-attachments/files/32865021/2022.emnlp-main.614.pdf | b10fa41a07177c40a45fd292d7e8147f4905fc41466fa3b61fed25271e2b281b |

研究报告：5 页；第 2 页六张独立图，第 3 页栅格表格，第 4–5 页原生表格。
论文：18 页；第 4、9、16 页真实图形，其余正文、参考文献及表格均不能由页面 Form 整页认领。

泛化与保护反例由 `test_pdf_form_containers.py` 动态生成，不依赖原文、字体名称、资源名称或固定页码进行角色识别。
