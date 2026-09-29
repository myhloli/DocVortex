# PDF 原生内核阶段 20：textpage 生命周期与字符几何物化

## 实现边界

本阶段以 Stage 19 提交 `be0b1a1` 为基线，只处理 Stage 19 报告确认的两个候选，不继续扩大 Path 迁移。私有协议从 26 升到 27；公开 `auto|python|rust` 计算选择和 `auto|legacy|session` 渲染选择保持不变。旧扩展在 `auto` 下仍显式回退，`rust` 下继续报协议不匹配。

新增页面级自有文本快照入口：Python 仅核验 `FPDFText_LoadPage`、`FPDFText_ClosePage`、`FPDFText_CountChars` 与既有字符/颜色函数 ABI，并把当前同库 page 句柄交给 Rust；Rust 在宿主 PDFium 锁内短暂加载、计数、读取并关闭 textpage，产物仍只保存纯数值，不延长 PDFium 句柄生命周期。原 textpage 参数入口保留为兼容测试和参考路径；非标准 page、旧扩展、ABI 不匹配或异常数值时继续返回能力选择并走既有 Python 参考。

`NativeTextSnapshot.geometry()` 的兼容 Python 字符物化保留完整字典形状、插入顺序和可变语义，同时减少重复工作：字体字典按 Rust 字体索引一次构建，字段名一次缓存，bbox 输入直接构造列表，origin/loose/tight 坐标一次转换为 Python 对象并由字符字段和对应侧表共享同一对象。公开 `PDFPageTextGeometry.chars`、tight/loose/origin 字典、行成员身份、pickle 结果和独立副本行为不变。

## 验证

- 新增页面级 textpage 入口与既有 textpage 入口的完整快照等价测试，覆盖真实加载/关闭路径和命中诊断；协议注册测试更新到 27。
- 32 PDF／299 页公开输出双跑 Stage 20 后与 Stage 19 保存的完整 ModelJson、MiddleJson、素材哈希和诊断逐字段一致，报告为 `public-correctness.json`。
- MinerU 当前 `dev@f504cff`：31 份 eligible Flash 文本完整输出一致；32 份实际 medium shared 完整输出一致。未修改 MinerU，未跟踪 `examples/` 保持原状。
- Rust/session 完整测试：5475 passed / 1 skipped。Python/legacy 完整测试：4769 passed / 707 skipped。Cargo workspace tests、Clippy `-D warnings`、rustfmt、Ruff check 和改动文件 format check 均通过。
- ABI3 wheel 为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`。在 CPython 3.14 独立依赖环境中，DocVortex 核心路径和 MinerU Flash 文本路径的 `auto|rust` 输出一致，协议 27，页面级 bridge 可调用。源码扩展与 wheel 扩展 SHA-256 均为 `77c16ff6c926daea4b8ec631f192ba0f3593791cd885391cbe13d4cdf6d862d8`。

## 性能与资源

正式计时均为每文档一次预热、五次热运行；公开 parse 按文档先基线后候选，MinerU 按文档交替方向执行，RSS 使用独立进程树采样。结果仅代表本机语料与当前 PDFium 环境，不外推托管环境。

| 链路 | Stage 19 基线 | Stage 20 候选 | 降幅 | 首轮最大耗时比 | 首轮最大 RSS 比 |
| --- | ---: | ---: | ---: | ---: | ---: |
| DocVortex 公开 parse，32 PDF | 14.966399 s | 14.923734 s | 0.29% | 1.06446 | 1.00480 |
| MinerU Flash 文本，31 PDF | 14.827657 s | 14.759856 s | 0.46% | 1.02284 | 1.00549 |
| MinerU medium shared，32 PDF | 14.738283 s | 14.601869 s | 0.93% | 1.07333 | 1.12712 |

首轮超过 5% 耗时的样本均反序复测：公开 parse 的 `annual_report_management_roles_table.pdf` 复测比 0.99650；shared 的 `demo2.pdf`、`mixed_elements_pages_03_06.pdf`、`中文论文3.pdf` 复测比分别为 1.01313、1.00109、0.97148。shared 首轮唯一 RSS 超过 5% 的 `mixed_text_layout_sample.pdf.xor` 反序复测比为 1.02646。所有退化均未持续超过 5%，完整输出均相等。

诊断层面，同一 32 PDF cProfile 中 `prepare_visual_evidence()` 从 0.703 s 降至 0.612 s；八样本焦点集的字符几何物化阶段从 Stage 19 记录的 0.257527 s 降至 0.226503 s（约 12.0%）。页面级 snapshot 在该焦点集实际执行 161 次、无回退。`_extract_owned_text_snapshot()` 本轮约为 0.529 s，与 Stage 19 的 0.525 s 接近，说明 textpage Python 包装不是原始快照的主要成本；后续应把字符 FFI 读取和规范化本身作为 Stage 21 候选。

## 后续状态

- 原始 2 倍性能目标仍未完成；本报告不把正确性或小幅实际收益表述为该目标完成。
- Stage 21 不应继续优化 textpage 包装层。剩余更大热点包括 `_detect_table_candidates()`／表格物化、`_document_requires_full_geometry()` 的逐行 Python 输入打包，以及 `_plain_source_records()`；应在新的 profile 基础上选择下一批批量迁移。
- 证据目录为 `output/pdf/native-kernel-20260929-stage20/`。本阶段分支不自动合并、发版或发布 PyPI。
