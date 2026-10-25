# R3B OFD 字形几何 Rust 批次验收报告

日期：2026-10-25。以R3A最终源码为基线，DocVortex版本保持0.6.1，私有原生协议39升级至40。

**R3B本机验收通过。** 四份目标文字几何压力输入的公开解析热耗时中位数之和下降 **49.81%**，46份累计输入完整输出一致。完整测试12634项通过，51份导出输入和同一ABI3 wheel的Python3.11/3.14安装验证通过。

普通真实OFD没有获得统一收益：主累计初测约+6.35%，反序复测约 **+4.65%（3.46ms）**，低于计划5%门禁但仍保留这项成本。16份真实综合输入合计接近持平；本报告不把目标压力收益推广为全部OFD的收益。

## 1. 选定范围

在冻结R3A上重新profiling，长TextCode和大量裁剪字符的成本集中在逐字矩阵组合、quad变换/外接框及裁剪。Delta的流式解释、复杂数值恢复和预算不是本批可回收的主要部分；真实嵌入字体初始化和阅读顺序也占有独立成本。

本批只迁移已解析数值后的连续字形几何：

- Rust核心：`rust/docvortex-core/src/ofd_text_geometry.rs`。
- PyO3边界：`rust/docvortex-python/src/ofd_text_geometry.rs`。
- Python适配与原调用链：`src/docvortex/analyzers/native/ofd/text.py`。

长度至少8的默认TextCode使用原生路径。每批最多256字，临时元数据、局部框和返回记录不随整份文档长度无限增长；原有origins与可见GlyphItem列表仍由Python拥有。短文本、手工整数裁剪框、定制字体/几何方法和字形构造方法保留逐字路径。

Delta解析及其pending重放/预算、原点顺序累加、`math.dist`、字体字符解析和`glyph_bbox`继续执行原Python实现。由Python计算一次字符旋转矩阵，再分批计算原先每字的两次Affine组合、四角变换、有限值外接框及非退化交集。Rust不访问字体、XML、包资源或Python回调，纯运算释放GIL。

角度收敛、`math.hypot`、行bbox合并、文字/字形预算、CGTransform、坐标继承、警告、阅读顺序和导出保持原有调用阶段。没有跨文档缓存，也没有新增Cargo依赖。

## 2. 数值与类型边界

Rust按原表达式顺序执行乘法与左结合加法，不使用融合乘加，也不简化乘零、矩阵平移或原点变换。最小/最大值仅在严格更小/更大时替换，保留Python首项优先及正负零；不使用会改变零值选择的浮点min/max捷径。quad中的非有限值、退化框和相切交集继续被丢弃。

初版适配器允许手工整数裁剪框。审查发现，旧Python的max/min会保留选中整数的类型，而原生f64传输会转为float；新增内部GlyphItem回归复现该差异。最终适配器只对全float裁剪框批量计算，其余输入保留原路径；真实OFD解析的常规裁剪框为浮点值。

初版 `candidate-source`、被停止的 `cumulative-pair-01` 和 `type-guard-pre-fix.log` 保留，不作为最终验收。最终Python适配器重新冻结为 `candidate-source-v2`，并完整重跑五轮基准；Rust二进制未变。见 `initial-type-guard-correction.json`。

## 3. 冻结身份

证据目录：`/Users/myhloli/Documents/Codex/docvortex-nonpdf-optimization-20261025/R3B`。下文路径均相对此目录。

| 身份 | 源码/资源/构建配置SHA256 | 扩展SHA256 |
| --- | --- | --- |
| R3A `../R3A/candidate-source-v2`，协议39 | `466ef1582d6eecc4cb61c849456f993222a4ab37c155423285d447dc00e12a41` | `05bb34756f83b0715fc59a8553de9c3614fa9e6df737b4090029998c0b60eaa3` |
| 最终 `candidate-source-v2`，协议40 | `23af1b72ea3a8e64da6ce7260b6880316382a93435bd193bd57a49fec25c500e` | `fbfb6b17bd1eff9308bf36940e7feb196dfc84d42d9add199c88d6d4148adb05` |

R3A扩展复用已验收的独立冻结构建。本批实际重新构建，日志 `build-extension.log`、`build-wheel-01.log`；wheel内扩展与最终计时二进制完全相同。

## 4. 正式性能与复测

在macOS ARM64、CPython3.14.4上执行无额外包装的公开 `parse(..., keep_model_json=True)`。完整输出序列化在计时之外；五轮AB/BA交替、逐文档独立进程RSS，期间未并行运行本任务的构建、测试、导出或其它正式基准。汇总为逐文档五轮中位数之和。

`corpus/manifest.json` 保留R3A的41份输入，新增长代码、多行仿射、裁剪、旋转四份压力场景及多TextCode继承夹具，共46份。双方共用一次冻结的原始字节和解析参数。

| 集合 | R3A | R3B | 耗时变化 |
| --- | ---: | ---: | ---: |
| 四份目标压力输入 | 0.17405s | 0.08735s | -49.81% |
| 全部13份OFD | 1.91536s | 1.82826s | -4.55% |
| 全部46份累计输入 | 5.19680s | 5.09720s | -1.92% |
| 16份真实综合输入 | 3.22194s | 3.21841s | -0.11%，接近持平 |

| 目标场景 | R3A公开parse | R3B公开parse | 耗时变化 |
| --- | ---: | ---: | ---: |
| 8000字长TextCode | 49.668ms | 24.519ms | -50.63% |
| 160行仿射TextCode | 81.967ms | 49.090ms | -40.11% |
| 8000字大量裁剪 | 30.205ms | 4.618ms | -84.71% |
| 3000字旋转/裁剪 | 12.210ms | 9.125ms | -25.26% |
| 小型多TextCode继承夹具 | 0.933ms | 0.864ms | 约-7.39%，绝对差约0.069ms |

普通真实OFD主累计为76.448→81.303ms，比率1.0635；额外五轮BA/AB反序公开计时为74.368→77.824ms，比率 **1.04647**。原始累计成绩保留，没有以复测覆盖汇总。另在同一运行时和二进制中切换冻结/当前文字函数作归因诊断，比率1.03362，完整输出相同；该诊断不替代正式两源码性能结果。它提示普通样本存在小幅适配/物化成本，不能归结为统一加速。

46份逐样本RSS比率均未超过1.05；本批四份压力场景约0.9816–1.0023。使用原有50ms采样，不能据此证明全部瞬时峰值或所有规模都降低RSS。临时批次有界不等于整个文档只分配256字的数据，结果和原点列表仍随有效输入增长。

首次调用另测14份输入，每版三轮独立解释器与空字节码缓存，全部完整输出一致，最大中位数比率 **1.00964**。计时前不调用backend_info加载扩展，包含真实parse触发的首次原生加载，排除整个解释器启动和预先公共包导入。

证据：`cumulative-pair-02`、`reverse-time-recheck-01`、`same-runtime-real-ofd-diagnostic.json`、`first-parse-01`、`performance-recheck-summary.json`。

## 5. 正确性和实际入口

`tests/_ofd_text_oracle.py` 冻结R3A原始文字实现，原文件SHA256为 `b641b2ee12c1d318c7b971c3eeffba11ad091226b895ef57862a7b2c03ee6ee1`。差分预期不调用新Rust几何函数。

128个固定种子、每组48字形逐个比较浮点 `hex()`，不使用近似容差。定向覆盖正负零、边界相切/一ULP交集、奇异矩阵、巨大/极小值、非有限值和反向局部框。完整文字构建比较GlyphItem、quad、原点、glyph_id、行字段及所有预算，覆盖7/8字阈值、255/256/257/513批次边界、坐标继承、首尾空白、CGTransform和预算错误顺序。

自定义Affine、字体解析方法、quad/外接框/裁剪方法、三角函数及GlyphItem构造方法均有原路径回归。真实OFD和长TextCode通过公开入口与冻结解析完整对照。

`runtime-path-probe.json` 实际公开入口记录如下：

| 输入 | 完成批次 | 输入字形 | 保留字形 |
| --- | ---: | ---: | ---: |
| 普通真实OFD | 11 | 138 | 138 |
| 真实issue121 | 7 | 94 | 94 |
| 长TextCode | 32 | 8000 | 8000 |
| 仿射多行 | 160 | 10240 | 5600 |
| 大量裁剪 | 32 | 8000 | 198 |
| 旋转/裁剪 | 12 | 3000 | 299 |
| 多TextCode夹具 | 2 | 32 | 32 |

短文字表格场景和纯向量issue245可以没有本批调用，不能声称全部OFD都走了字形批次。表中保留数量是内部GlyphItem数量，不是公共输出的全篇字符数。所有探针与正式完整输出摘要一致。

## 6. 测试、导出与视觉

- 聚焦检查289 passed、13 skipped；整数裁剪框补充的原路径spy检查通过。
- 最终完整测试：**12634 passed、14 skipped、6 warnings**，日志 `full-pytest-01.log`。
- Rust43项单元及8项集成通过，workspace全部目标Clippy `-D warnings`、Cargo格式、35份改动/新增Python的Ruff检查/格式和 `git diff --check` 通过。
- 新增函数及测试辅助函数的中文说明无缺项，见两个 `function-comment-audit-*.json`。

`exports-run-01` 对51份输入执行102个独立解析进程，完整ModelJson、MiddleJson、素材及diagnostics相同。压力输入比较完整解析，其余26份各实际写出七种格式：

- **364次实际写出、52次bundle回读和HTML再渲染成功**，逐项核对主文件、旁文件、素材及渲染诊断。
- **26对PDF原始字节相同**，双方各141页的文字、尺寸和像素签名相同。
- 14对DOCX仅ZIP时间字段差异，逐成员及其余原始字节一致。导出错误为零，没有将前后相同错误计作通过。
- Poppler100dpi另渲染普通真实OFD正文、真实中文表格及多TextCode仿射/继承夹具，共六张PNG，逐对相同；已查看三张候选页面，未见新增乱码、裁切、内容丢失或表格归属变化。见 `visual-review/pdf-review.json`。

已有非致命公式、素材和向量回退诊断前后相同。本批不修复这些既有质量边界。

## 7. 安装与交付边界

wheel：`dist/docvortex-0.6.1-cp311-abi3-macosx_11_0_arm64.whl`，SHA256为 `723c6a399d96796d8ca4bfacd235f231536d7fd2203d67e816c77628eb17646a`。

同一wheel分别安装到新Python3.14.4和3.11.15环境，各46份完整输出通过；实际导入来源、协议40、CLI、432个包文件及431个非原生文件均核对成功。Python3.11先在同一3.11依赖环境对照两份冻结源码，再验证实际安装包输出；其单轮源码回放与测试/导出并行，时间与RSS不作为性能证据。

两个运行时各通过公开PDF全页/选页的完整输出和原生入口烟雾验证。证据为 `wheel-acceptance-01`、`wheel-acceptance-311` 及各自 `pdf-smoke/summary.json`。依赖沿用对应Python已有site-packages，未重新解析全套依赖，原 `.venv4` 和B08产品安装未被替换。

验收仅覆盖macOS ARM64两个Python端点。跨平台CI、提交、推送、PR、合并、发布均未执行。

## 8. 状态与后续

工作区：`/Users/myhloli/.codex/worktrees/nonpdf-optimization/docvortex`，分支 `codex/nonpdf-optimization`。此前批次和本批修改均保留。

`R3B-production.patch` 以冻结R3A为边界单列六个生产文件；`final-working-tree.patch` 保存累计修改。回退本批需恢复R3A源码和协议39扩展，并同步恢复原生注册及协议测试。

R3的表格与字形几何阶段已完成本机验收。下一批先按新profile评估OFD阅读方向/基线投影与原始字形边界复用；HTML/EPUB Span、旧DOC/PPT区间和CSV构造继续作为独立后备项。B5继续跳过。
