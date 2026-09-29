# PDF 原生内核阶段 21：表格脚本与全文几何边界批量化

## 实现边界

本阶段以 Stage 20 提交 `d504c5e` 为基线，不再继续 textpage 或 Path 迁移。私有协议从 27 升到 28；公开 `auto|python|rust` 计算选择、`auto|legacy|session` 渲染选择、公共 SDK 和输出 schema 均保持不变。

表格 HTML 物化复用页面级 `NativeScriptEvidence`：`_collect_document_sources()` 已生成的 owned 身份映射继续传入表格恢复链路。普通 0 度、无 fraction/inline 特殊证据且字符身份完整的 cell visual line 被合并为最多 8192 字的 Rust 批次；原生角色只要出现非 body，整 cell 回退既有 `_script_line_char_roles()` 参考路径，避免改变表格二维行合并后的精炼语义。诊断新增表格 owned 批次、命中行数和回退 cell 数。

表格候选的 marker 输入校验从每个 corridor 改为每个 `_RuleCandidateContext` 一次。同一方向候选组共享 marker-safe 结果和 source line 索引；重复 row、特殊字符、非有限坐标和 Python backend 仍走原参考路径。

新增页面级 `NativeGeometryEvidence`：Rust 一次从自有 text snapshot 准备 source/loose/tight/origin、rotation、字体 run key、字号、宽粒度文字类别和 anchor 标记。Python 主链路只传行成员索引和行元数据；`NativeGeometryRuns` 在全文内分配稳定 run ID，`NativeGeometryRisk` 连续累积布局/样式风险。解释器相关的字体族归一化、字号/字重取整和 Unicode 文字类别只在不同值上调用。身份缺失、特殊输入、旧扩展或规则替换时整本文档回退原逐行参考路径。

## 验证

- 32 PDF／299 页公开回放每份双跑；ModelJson、MiddleJson、素材哈希和诊断与 Stage 20 完整输出完全一致。
- MinerU 当前 `dev@f504cff`：31 份 eligible Flash 文本完整输出一致；32 份实际 medium shared 完整输出一致。MinerU 工作区仅保留原有未跟踪 `examples/`，未修改其源码。
- Rust/session 完整测试：5465 passed / 14 skipped。
- Python/legacy 完整测试：4756 passed / 723 skipped。
- Cargo workspace tests、Clippy `-D warnings`、rustfmt、Ruff check 和改动文件 format check 均通过。
- ABI3 wheel 为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`。CPython 3.14 独立环境中 DocVortex 核心路径和 MinerU Flash 文本路径的 `auto|rust` 输出一致，协议 28，geometry evidence API 可用。源码扩展与 wheel 扩展 SHA-256 均为 `6266f409337d742153dc5bc0727d3b28f32a8f4cb210d8a5ea6e06cc650d3deb`。

公开正确性诊断中，表格 owned 路径执行 368 批／27,602 行／932 个 cell 回退；geometry evidence 执行 598 页／34,990 行／0 回退。32 PDF 单遍完整 cProfile 中，geometry evidence 执行 299 页／17,495 行，表格 owned 执行 184 批／13,801 行。

## 性能与资源

正式计时均为每文档一次预热、五次热运行；公开 parse 按文档先基线后候选，MinerU 按文档交替方向，RSS 使用独立进程树采样。结果仅代表本机语料与当前 PDFium 环境。

| 链路 | Stage 20 基线 | Stage 21 候选 | 首轮总降幅 | 首轮最大耗时比 | 首轮最大 RSS 比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DocVortex 公开 parse，32 PDF | 15.943439 s | 15.513413 s | 2.70% | 1.02077 | 1.03600 |
| MinerU Flash 文本，31 PDF | 15.746155 s | 15.304521 s | 2.80% | 1.02846 | 1.04413 |
| MinerU medium shared，32 PDF | 16.010728 s | 17.845207 s | -11.46% | 1.58188 | 1.00490 |

shared 首轮有 4 个样本耗时超过 5%，均已按脚本自动反序复测：

- `small_ocr.pdf`: `1.09802 → 0.98395`
- `engineering_process_restrictions_table.pdf`: `1.51380 → 0.99771`
- `pollutant_discharge_tables.pdf`: `1.58188 → 0.88237`
- `quarterly_report_financial_tables.pdf`: `1.12287 → 0.90017`

四个样本均无持续退化。仅把这四个已触发样本替换为反序复测值作诊断汇总时，shared 总时间为 `16.174975 s → 15.777589 s`，约 2.46% 改善；该补充口径不替代首轮正式总表。所有 benchmark 完整输出均相等，公开 parse、Flash 和 shared 均无持续超过 5% 的耗时或 RSS 退化。

十样本表格重载焦点 profile 中：

- 墙钟：`11.889497 s → 10.338891 s`，约 13.03%。
- `_cell_script_roles`: `1.969974 s → 0.040009 s`
- `_prepare_table_core_rows`: `0.542125 s → 0.026794 s`
- `_document_requires_full_geometry`: `0.467145 s → 0.050283 s`
- 三项合计：`2.979244 s → 0.117086 s`，低于 `2.80 s` 门槛。

## 后续状态

原始 2 倍性能目标仍未完成。Stage 21 后的 32 PDF cProfile 主要剩余热点为 `_detect_table_candidates()` 约 `6.821 s`、`_materialize_table_blocks()` 约 `5.384 s`、`_infer_text_lanes()` 约 `3.252 s`、`build_document_geometry_plan()` 约 `3.626 s` 和 `_merge_owned_table_candidates()` 约 `2.224 s`。其中 geometry plan 的准入扫描已下降，剩余成本主要在确认布局风险后的 canonical 样本构造。Stage 22 应优先从表格候选合并/物化与 lane inference 重新 profile 后选择，不应继续扩大本轮 owned script/geometry 范围。

证据目录为 `output/pdf/native-kernel-20260929-stage21/`。本阶段不自动合并、发版或发布 PyPI。
