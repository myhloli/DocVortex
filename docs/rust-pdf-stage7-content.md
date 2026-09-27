# PDF 原生内核阶段 7：正文内容组装

DocVortex `f8ae7f2`、MinerU `8adde38a`，私有协议 14。已有共享上下标侧车时，Rust 自有字符连续完成归属、原顺序 PUA 统计、附加符合成、宽度中位数、词间空格和正文组装，输出每个 span 的文本及 PUA 信号。空框保留原内容；Python OCR 决策和结果结构不变。需要单独脚本检测或存在自定义规则、特殊值时使用明确参考入口。

Unicode 空白、类别、NFC 和 strip 继续采用运行中 CPython 的语义；仅按唯一字符/组合查询，不改成不同版本 Unicode 表。七种既有连字替换维持原契约，不引入全局 NFKC。

## 证据

- 32 PDF / 299 页真实 medium/auto 全量审计，模型输入和完整输出一致。
- DocVortex 112 项、MinerU 139 项相关检查通过。新增 24 组随机框的内容对照以及附加符、连字、PUA、Unicode 空白和控制字符真实 PDF 检查。Rust workspace 与严格 clippy 通过。
- 正式运行记录 1332 次原生正文组装调用，证明主路径实际使用了新接口。
- 同一原始 Rust 基线、逐文档一次预热及五次正式测量、交替版本，内存独立测量。

| 指标 | 实测 |
| --- | --- |
| 基线中位数之和 | 24.842366 秒 |
| 候选中位数之和 | 17.814200 秒 |
| 耗时降低 | 28.2910% |
| 最大同时刻进程树 RSS 比 | 93.2696% |
| 超过 5% 的单样本耗时退化 | 0 |
| 完整结果一致 | 32 / 32 |

原始证据：`output/pdf/native-kernel-20260927/shared-stage7/report.json`、`shared-stage7-summary.json`、`shared-audit-stage7-summary.json`；候选冻结在 `candidate-stage7` / `mineru-stage7`。不将不同批次的基线波动算作新阶段的精确增益。

仍未达到耗时降低 50% 的验收目标；本报告只验收 medium 共享链路。尚未用协议 14 重新验收公开 parse、Flash 或跨平台 wheel。下一批继续复用页面级上下标输入，再按实测热点推进样式和分类读取。未切换默认、合并或发布。
