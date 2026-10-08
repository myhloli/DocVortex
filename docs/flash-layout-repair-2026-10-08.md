# Flash 原生 PDF 布局修复验收 · 2026-10-08

两份原件的全部 11 项问题通过验收，48 页原页与布局均已视觉复核。修复使用字符几何、
数值列、表头、字重、留白和局部成员关系，不依赖文件名、页码、块号或特定原文句子。

## 修复范围

| 原件及物理页 | 问题 | 验收结果 |
| --- | --- | --- |
| 季报 14–16、20 | 财务表被判为目录 | 项目与四个金额列、四层表头完整恢复，移除 index 回退 |
| 季报 18、19、21 | 续页表格漏检 | 完整五列表格，含空值、括号负数和末尾汇总行 |
| 季报 17 | 局部数字表与目录重叠 | 五列统一认领，全部项目及末尾利润行保留 |
| 季报 12 | 两张原生表与正文被合为图片 | 两表独立恢复，中间说明仍为正文，原有流动性表保持 |
| 季报 3、4 | 矢量候选接受欠分割网格 | 四列指标表及五列原因表，保留原因字段跨行关系 |
| 国标 15 | 章节正文误判代码、表题重复 | 两组章节标题和正文恢复，表 2 标题唯一归属 |
| 国标 10–12 | 术语标题和定义串块 | 22 个编号与中英文术语分别成标题，定义和来源说明保持正文 |
| 国标 10 | 术语编号误判页眉 | 3.1 归入术语标题，标准号页眉保持，公开导出保留编号 |
| 国标 20 | 正文半句升格为全文标题 | 连续正文完整恢复，无跨行续词断裂 |
| 季报 7 | 负债说明升格为全文标题 | 完整正文和金额保留，无中途 doc_title |
| 国标 9、24、25 | 标题带顺序倒置、A.1 漏分类 | 题名和附录标题先行；A.1 完整 equation，主体先于编号，B.1 保持 |

## 回归证据

- Python 全套：9740 通过、14 跳过；新增原件与泛化用例：54 通过。
- Rust 工作区：16 项核心与契约测试通过；匹配源码的原生扩展已构建。
- 完整语料：按解码源哈希去重，共 192 份、687 页；其余 190 份、639 页公开输出保持不变。
- Python／Rust 的 ModelJson、MiddleJson、HTML、Markdown 逐文件字节一致；auto 的两份原件 48 页一致。
- 13 张问题表按原页独立转写真值检查 1018 个逻辑网格位置，包含表头、金额、空值及合并单元格。
- 原页、修改前和修改后标框已逐项复核；21 个变化页接受为改善，公式 A.1 裁图含完整点号与上下标。
- 完整语料通过导出、Bundle 回读及图片解码；最终 MiddleJson 无内部诊断字段泄露。
- MinerU 公开 Flash 入口：两份完整原件分别执行 txt 和 auto，四次运行确认实际 Flash 路由及表格、公式结果。

## 公共 API 审查

对冻结源码与修复源码枚举公开清单、显式导出、函数及类方法签名、Pydantic JSON Schema，
42 个模块、343 个符号及 1508 个公开类方法签名均保持一致，无遗漏的公共 API 更新。
公开的区域表格恢复入口也逐格通过同一份独立真值，输入文字与几何证据没有被修改。
81 项公开接口专项测试通过。

`PUBLIC_API`、块类型、ModelJson／MiddleJson、结果和素材格式、异常边界均兼容。
本轮不改变版本号或原生协议 32；Python 与原生扩展须使用匹配源码。SDK 行为记录见
[公共 SDK 文档](sdk-0.4.md#flash-原生布局修复2026-10-08)。

## 可复现样本

| 样本 | 页数 | 源文件 SHA256 |
| --- | --- | --- |
| [standard.pdf](../tests/unittest/pdfs/flash_review_20261008/standard.pdf) | 27 | `3e77032241e182398cabb4d59c7730bb3622d3444227a732cf71718930e3ad9e` |
| [quarterly_report.pdf](../tests/unittest/pdfs/flash_review_20261008/quarterly_report.pdf) | 21 | `eac7aa5e95661e28e222eddd89e94965d4e8874e436e945cf3ac20919dbf6676` |

独立表格真值在 [flash_review_20261008_cells.json](../tests/fixtures/flash_review_20261008_cells.json)，
回归断言在 [原件用例](../tests/unittest/test_flash_review_20261008.py) 和
[泛化用例](../tests/unittest/test_flash_review_20261008_generalization.py)。

本地完整证据保留在 `output/pdf/flash-repair-20261008/`：`validation.json`、
`findings.accepted.json`、`public-surface-before.json`／`public-surface-after.json`、
`public-tables-final.json`、测试日志及 `report.html` 前后画廊。该运行目录不纳入 Git。
原审阅目录 `output/pdf/flash-audit-20261008/` 保留不变。

验收源码指纹（`src` 内 Python 文件路径及内容）：
`4f644826f858c3f48988c9c2a9c7d4879fc2e0459e7ce10aa90df1f2f9ec436e`。
