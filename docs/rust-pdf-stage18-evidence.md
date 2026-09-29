# PDF 原生内核阶段 18：文本可见性与证据复用

## 实现边界

私有协议 25 将 Flash 可见文本对象遍历、render mode、颜色 alpha 和裁剪转换合并到一次 Rust 读取。颜色读取失败仍按 Python 参考语义处理为 alpha 255；模式 3/7 隐藏；空裁剪置不可见；ABI、非标准页面对象或扩展能力不匹配时整页回退参考实现。`bridge_info()` 新增 `pdfium_text_visibility_bridge_calls` 与回退原因，公开 `auto|python|rust` 和渲染选择不变。

自有视觉证据减少临时复制：`statistics::typography()` 直接借用字体族数组，median 输入不再克隆切片；视觉 run 的成员、bbox、字体 ID 和字重集合按容量预分配；Rust 预计算正数字体尺寸中位数并随私有 metrics 传给 `_LineItem`，后续上下标配对不再逐字符重扫 Python 字典。该缓存字段不参与数据类相等比较，公共输出形状不变。

Path 双侧消费改为 `_path_object_evidence()`：每个 Path 只读取一次绘制状态、描边宽和原始子路径，只执行一次对象到页面视觉坐标转换，同时生成绘图线候选与 `PDFPathInfo`。独立入口保留，测试注入的非标准提取器仍走旧分支；单个损坏 Path 继续被隔离。本阶段没有迁移链接判断、复杂 Path 全量算法或最终 Flash 规则。

## 验证

- 新增/更新的焦点检查覆盖 TEXT 可见性与 Python 参考在 0/90/180/270、嵌套 Form、裁剪、ABI 回退、错误传播和无效地址下逐字段一致；Path 联合消费确认每对象绘制状态只读取一次；字号缓存验证异常值回退。
- Rust/session 完整测试：5473 passed / 1 skipped。Python/legacy 参考完整测试：4768 passed / 706 skipped。Cargo workspace tests、Clippy `-D warnings`、rustfmt、Ruff check/format 均通过。
- 32 PDF／299 页公开回放各两次，基线 `5d2162a` 与候选完整 ModelJson、MiddleJson、素材哈希、诊断逐字段一致；候选 598 次实际命中 Rust TEXT visibility，无回退。
- MinerU 当前 `dev@ee87e8e`：31 份 eligible Flash 文本完整输出一致；32 份实际 medium 共享链路完整输出一致。共享链路沿用 Stage 5 真实模型调用录制，但不再把候选输出强制比作 0.5.2 前录制哈希，而是直接比较当前 0.5.7 基线与候选输出。
- ABI3 wheel 构建为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`；CPython 3.14 ABI/kernel 检查通过，独立安装环境中 Python/auto 与 Rust 输出一致。最终源码扩展与 wheel 二进制 SHA-256 均为 `60fad12cebd42332cbc436091de3c7a169b8f4b1ca02ba6ebb20d4a02abf1371`。

## 性能与资源

所有正式计时为每文档一次预热、五次热运行，基线/候选按文档交替，并独立采样进程树 RSS。结果仅代表本机与当前 PDFium 环境。

| 链路 | 基线合计 | 候选合计 | 降幅 | 最大单档结果 |
| --- | ---: | ---: | ---: | --- |
| DocVortex 公开 parse，32 PDF | 15.621533 s | 15.318385 s | 1.94% | 耗时 1.0309；RSS 1.0023 |
| MinerU Flash 文本，31 PDF | 15.698599 s | 15.233064 s | 2.97% | RSS 1.0084 |
| MinerU medium 共享，32 PDF | 14.827323 s | 14.760071 s | 0.45% | 首轮耗时 1.0754，反序 1.0113，非持续 |

公开 parse 首轮最大耗时退化 3.09%、最大 RSS 增加 0.23%，均低于 5% 持续门槛。共享链路 `caibao1.pdf` 和 `fund_manager_profile_table.pdf` 首轮超过 5% 后反序复测分别降至 1.13% 和 2.19%，判为非持续；`pollutant_discharge_tables.pdf` RSS 为基线 104.28%，低于既有 130% 资源门槛。八样本焦点集公开 parse 合计降低 3.38%。

来源收集独立诊断显示：`_extract_owned_text_snapshot` 从 0.8347 s 降至 0.5448 s，`_extract_page_paths_and_lines` 从 0.4046 s 降至 0.3354 s，`_build_native_line_items_from_records` 从 0.1507 s 降至 0.1043 s，全阶段 `_collect_document_sources` 从 0.7928 s 降至 0.7514 s。单次 cProfile 不替代正式中位数。

## 下一批优先级

候选 cProfile 中最大项仍是 `NativeTextSnapshot.prepare_visual_evidence()`（约 0.693 s）、Path 联合证据（约 0.489 s）和原始 text snapshot 读取（约 0.347 s）。下一轮应先细分 `prepare_visual_evidence()` 内的字体归一化、Unicode 属性、行分组和最终 Python 物化，再将 Path 子路径/属性批量化；继续保留同一 PDFium 库和串行调用边界，不凭阶段总量预设可消除成本。

证据目录为 `output/pdf/native-kernel-20260929-stage18/`。分支未合并、未发布；原始 2 倍目标仍未完成。
