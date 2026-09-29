# PDF 原生内核阶段 19：Path 双侧证据批量化

## 实现边界

本阶段以已完成的 Stage 18 分支提交 `634aabe` 为基线，不重做 TEXT 可见性、render-session、tight spacing 或 Stage 18 的视觉证据复用。私有协议从 25 升到 26，公开 `auto|python|rust` 和渲染 `auto|legacy|session` 选择保持不变；旧扩展在 `auto` 下仍显式回退，`rust` 下继续报协议不匹配。

新增原生 Path evidence bridge：一次遍历页面/Form 内全部 PATH 叶子，复用同一组 PDFium 函数指针，批量读取子路径、绘制状态、填充/描边颜色、描边宽、Form 矩阵与累积裁剪。Rust 内一次完成对象坐标到页面视觉坐标转换，同时产出未合并绘图线和 `PDFPathInfo`；Python 只物化公开 dataclass 并保留既有共线合并规则。描边宽度中的 `hypot` 调用绑定当前解释器的 `math.hypot`，避免跨实现最后一位的浮点差异。颜色读取失败沿用 alpha 255；损坏 Path 段按参考实现跳过；ABI、非标准页面或旧扩展缺失时整页回退既有 Python 参考。诊断新增 `pdfium_path_evidence_bridge_calls` 与回退原因。

`NativeTextSnapshot.prepare_visual_evidence()` 增加仅在 `DOCVORTEX_PROFILE_VISUAL_EVIDENCE` 显式开启时累计的阶段计时，可区分字体归一化、Unicode 属性、视觉 run、字符几何物化和最终 run 物化。正式路径不输出计时，不改变公共 SDK、输出 schema 或行/字符成员关系。

## 验证

- Path 联合证据新增原生/Python 参考逐字段且按顺序一致的差分；Python 双消费测试继续禁用原生桥接，验证每个对象解码一次；原生桥接报告区分真实执行与回退。
- 32 PDF／299 页逐页调用原生 Path evidence 后强制走 Python 参考：绘图线与 `PDFPathInfo` 全量相等，原生命中 299 次、无回退。
- 32 PDF／299 页公开回放各两次，Stage 18 `634aabe` 与本阶段完整 ModelJson、MiddleJson、素材哈希和诊断摘要全部一致。
- MinerU 当前 `dev@ee87e8e`：31 份 eligible Flash 文本完整输出一致；32 份实际 medium 共享链路完整输出一致。共享链路继续直接比较当前双源输出，不误称匹配 Stage 5 旧录制哈希。
- Rust/session 完整测试：5474 passed / 1 skipped。Python/legacy 完整测试：4769 passed / 706 skipped。Cargo workspace tests、Clippy `-D warnings`、rustfmt、Ruff check 和改动文件 format check 均通过。
- ABI3 wheel 构建为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`；独立安装环境中 CPython 3.14 的 Python/auto 与 Rust 输出一致，协议 26，Path bridge 实际命中。源码扩展与 wheel 扩展 SHA-256 均为 `f6dcdb5654cd30c3617aecbc22fd57ec3c1a49b8aa6d1e13ef2e3df062ec0ab2`。

## 性能与资源

正式计时均为每文档一次预热、五次热运行；公开 parse 按文档先基线后候选交替执行，MinerU 按文档交替方向执行，并另用独立进程采样进程树 RSS。结果仅代表本机语料与当前 PDFium 环境。

| 链路 | Stage 18 基线 | Stage 19 候选 | 降幅 | 最大单档结果 |
| --- | ---: | ---: | ---: | --- |
| DocVortex 公开 parse，32 PDF | 15.484642 s | 15.202684 s | 1.82% | 耗时 1.0020；RSS 1.0330 |
| MinerU Flash 文本，31 PDF | 15.335368 s | 15.045106 s | 1.89% | 耗时 1.0266；RSS 1.0165 |
| MinerU medium 共享，32 PDF | 15.531999 s | 14.566413 s | 6.22% | 耗时 1.0411；RSS 1.0142 |

公开 parse 无任何首轮超过 5% 的耗时退化；最大耗时比率 0.20%，最大 RSS 增加 3.30%，低于持续退化与资源门槛。Flash 最大耗时比率 2.66%，无持续退化；medium 最大首轮耗时比率 4.11%，未触发反序复测。完整输出均相等。

同一 32 PDF cProfile 对照显示，`_extract_page_paths_and_lines()` 从 0.940791 s 降至 0.129739 s（约 86.2%）；Stage 18 的 `_path_object_evidence()` 逐对象热点在本阶段候选中消失。`_collect_document_sources()` 从 4.119941 s 降至 3.171536 s。单次 cProfile 不替代正式中位数。

对八样本焦点集显式开启 `DOCVORTEX_PROFILE_VISUAL_EVIDENCE` 后，161 次调用累计：字体准备 0.008330 s、Unicode 属性 0.043204 s、视觉 run 0.041006 s、字符几何物化 0.257527 s、最终 run 物化 0.008681 s，总耗时 0.319887 s。字符几何物化约占 80.5%，是 Stage 20 的首要候选；原始 text snapshot 读取约 0.349 s 仍是第二大候选。

## 后续状态

- 原始 2 倍性能目标仍未完成；本报告不把正确性或单阶段收益表述为该目标完成。
- Stage 20 不应继续扩大 Path 范围。优先研究延迟/分批物化 `PDFPageTextGeometry.chars`，并细分 `Bbox`/字典构造与后续消费者是否可共享原生字符对象；其次研究直接持有 PDFium textpage 的原生 snapshot 读取。
- 证据目录为 `output/pdf/native-kernel-20260929-stage19/`。本阶段分支不自动合并、发版或发布 PyPI。
