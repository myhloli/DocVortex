# PDF 原生内核第 10–11 批：原始分类与渲染并行度

## 交付内容

DocVortex 实现检查点 `34fb8c5`，私有协议 16；MinerU 生产接入代码维持本轮已验证版本，测试编码修复检查点为 `739ad39f`。按用户要求全程单代理，原始冻结 Rust 基线的进程树峰值 RSS 上限为 130%。

- PDFium 原始 Unicode、generated 标志、Unicode map error 和字体字节读取进入 Rust，统计独立于 canonical 去重字符。字体 UTF-8 ignore、规范化名称碰撞以及每种统计各自的字典键首次出现顺序均保持参考行为。
- 原始分类快照不持有 PDFium 指针，文本页关闭后仍可读取；每次导出独立字典。原生计算失败明确抛出，不进入通用 OCR 回退。
- 上下标记录按 canonical 字体 ID 复用等价类，消除逐字符字体名复制/哈希。
- 整页渲染从原来每 worker 至少 30 页调整为至少 4 页，仍使用既有最多 3 进程的共享预算；裁图保持原策略，DPI、编码器、像素与结果顺序不变。

分类的独立原始统计快照不等于分类与正文已经共享全部原始读取；文档/页面 Rust 所有权、矢量快照和完整 Flash 内核仍需后续迁移。

## 正式热运行验收

冻结候选：`output/pdf/native-kernel-20260927/candidate-stage11` / `mineru-stage8`。基线为 DocVortex `3eef2d1` / MinerU `cabe6e34`，同样启用旧 Rust。逐文档预热一次、正式五次，逐文档交替版本；内存独立测量。候选显式使用 session，基线使用 legacy。Profiler、构建、测试和真实模型录制均在正式计时之外执行。

| 范围 | 样本 | 基线中位数总和 | 候选中位数总和 | 耗时降低 | 最大进程树 RSS 比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 公开 parse() | 32 | 21.999844 s | 18.971245 s | 13.77% | 108.66% |
| MinerU Flash 自动文本 | 31 | 23.925293 s | 19.198173 s | 19.76% | 108.30% |
| 实际 medium 共享 PDF | 32 | 25.025109 s | 14.663202 s | 41.41% | 93.08% |

完整结果一致，三条链路均通过无持续超过 5% 退化及 RSS 130% 门槛。公开入口的 `manufacturing_facilities_cross_page_table.pdf` 首次为 +5.49%，反序复测为 -4.17%，未构成持续退化；首次值保留在总和中。`small_ocr.pdf` 仍分类为 OCR，因此不计入 Flash 文本速度。

**三条链路均未达到耗时降低至少 50% 的最终目标。** 共享路径尚需在本批基线下再减少约 2.15 秒总和。不同轮次的基线波动不能算作增量收益。

## 冷启动与连续处理

32 份实际 medium/auto 输入，每次观测启动新解释器、每份五次并交替版本：逐文档中位数总和从 44.973797 秒降到 35.309844 秒，降低 **21.49%**，没有首次或持续超过 5% 的退化。计时包含解释器、导入、输入及冻结模型结果加载、字体初始化和 worker 启动直到首次分析返回；不包含真实模型计算、返回后验证和退出清理。没有用热运行替代冷启动，也未从总耗时扣除模型结果加载。

独立的连续栅格化内存检查覆盖 8 份输入，窗口 16 页，每份预热一次再运行五次。所有样本的进程树峰值 RSS 比在 22.32%–74.79%，worker 身份稳定、输入与像素资源释放检查均通过。回收后驻留差值为 16 KiB–8.21 MiB；这是有限次数测试，不能据此证明无限运行没有泄漏。原始证据为 `cold-stage11/report.json`、`cold-stage11-summary.json` 和 `render-memory-stage11-summary.json`。

## 质量与兼容性

- 32 PDF / 299 页的原始分类字段和字典顺序逐页与 Python 参考一致。
- 32 份实际 medium 模型输入与完整输出严格回放一致；公开 ModelJson、MiddleJson、素材摘要和诊断全等。
- 协议 16 完整测试 5165 passed / 1 skipped；原 Python 分类路径 21 passed。真实多进程渲染追加 29 项、宿主相关检查 32 项通过。
- 实际安装 ABI3 wheel 检查通过；最终 wheel 的渲染、分类和快照追加检查 70 项通过，双 wheel 包文件、来源及扩展已逐项校验。
- [DocVortex CI](https://github.com/myhloli/DocVortex/actions/runs/36284975783) 39 项全部通过；[原生 wheel 矩阵](https://github.com/myhloli/DocVortex/actions/runs/36284975780) 全部通过；[双仓联合安装矩阵](https://github.com/myhloli/Magic-PDF/actions/runs/36285069477) 9 个组合全部通过。

原始报告分别位于 `shared-stage11`、`public-stage11`、`flash-stage11`，摘要为对应 `*-summary.json`，执行顺序和退出状态保存在 `stage11-execution.json`。三样本增量试测仅用于选方案，不代替上述全量验收。

## 后续顺序

优先整体迁移全文几何计划、表格候选展开/合并和最终布局，详见 [P4 执行计划](pdf-native-kernel-p4-execution.md)。默认未切换，未合并、未发布；高档位全量真实回放、实际 WebUI 和最终后端退出仍未完成。
