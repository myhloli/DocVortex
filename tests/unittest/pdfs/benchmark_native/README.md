# 原生 PDF benchmark 回归原件

这些文件直接取自本地 `opendataloader-bench/pdfs/` 的单页 PDF，没有重新生成或改写。
原 benchmark 编号、路径和 SHA256 保存在 `tests/fixtures/benchmark_native_manifest.json`；测试先检查原件指纹，再解析原件。

| 原件名称 | 文档编号 | 原页事实 |
| --- | --- | --- |
| dotted_contents | 18 | Contents 是唯一标题，24 条点线目录完整保留 |
| employment_charts | 38 | 图内数据、横轴和图例按物理位置排列 |
| migration_charts | 76 | 旋转日期标签沿横轴从左至右排列 |
| microscope_steps | 115 | 第一编号步骤及 a–f 子项先于第二步骤 |
| replication_sidebar | 118 | 两个粗体侧栏标签独立于后续简介 |
| portfolio_sparse_table | 130 | 底色表头支持六行三列 |
| linked_initiatives_table | 147 | 超链接下划线不能成为额外行界 |
| competence_filled_table | 150 | 六行两列，Learning Outcomes 跨两列 |
| mineral_units_table | 166 | 七行两列，单位行保留 |
| erosion_multilevel_tables | 170 | 两张表分别为八行六列、九行五列 |
| recommendation_panels | 183 | 总标题和三面板标题独立，小字号简介保留为正文 |
| search_value_panels | 184 | 三栏各自按指标、子标题、说明排列 |
| training_grouped_table | 187 | 六行七列，三层分组表头由局部横线证明跨格 |
| ocr_performance_panels | 199 | 两个并列面板标题独立于图表 |

断言依据原页的文字、字形、绘制线和布局，不依据候选输出自动更新。
相应变形及缺证据反例位于 `test_benchmark_native_rules.py`。
位图表格 110、122、位图标题 148，以及轮廓字标题 141 属于本次原生解析边界之外，未引入 OCR 或视觉模型。
