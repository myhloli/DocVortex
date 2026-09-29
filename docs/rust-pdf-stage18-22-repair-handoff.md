# Rust 阶段 18～22 修复任务交接状态（2026-09-30，已完成）

用户更换模型后要求“继续任务”，本次已恢复并完成全部实现与验收。**没有待运行的验收任务，也没有应继续推进的性能目标。** 以下先记录最终状态，再保留暂停时的历史交接记录；正式结论见 [rust-pdf-stage18-22-repair.md](rust-pdf-stage18-22-repair.md)。

## 最终完成状态

- 五处行为差异全部修复，Python／Rust 私有协议 29；按三链路完整消融结果，已回退阶段 20 Rust textpage 生命周期迁移，保留字符几何物化优化。
- 最终对 v0.5.7 三轮配对改善中位数：公开 **9.473%**、Flash **10.207%**、shared **4.367%**。285 对正式配对及 2 对反序复测业务输出全部一致，无持续单文档退化、无链路净退化，RSS 均低于 130%。
- 32 份 shared、两构建共 64 个严格模型输入审计全部通过，不计入性能数字。
- Rust/session 完整 pytest：**5479 passed / 14 skipped**；Python/legacy：**4765 passed / 728 skipped**，未 deselect 用例。Rust 相关回归 264 passed；Python 相关回归 120 passed / 144 skipped。
- Cargo workspace 15 passed；Clippy、rustfmt、全仓 Ruff check、src 与修改测试 Ruff format、证据脚本 Ruff check、`git diff --check` 全部通过。
- ABI3 wheel 在独立 CPython 3.14.4 venv 安装，不借用 `.venv4` site-packages。auto／rust 均实际命中协议 29、各 13 次文本快照命中；Python／Rust 完整输出一致，auto／rust 输出一致，安装版相关回归 **264 passed**。
- wheel：`docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`，SHA-256 `63f5327fb7f4b3c835923644fd750347eea7c161264092049784016925cb9d6c`。wheel 内扩展与最终性能版 SHA 相同：`a7e86593dc078d50bbe3fe52cad5ca1154453a7e5daeeb41a507cbaac1a24029`。
- 工作区路径与下文相同。本文件记录发版前的修复验收，用户随后要求提交、推送并发布 0.5.8；远端状态以 main 历史、`v0.5.8` 标签和 GitHub Release／PyPI 为准。原阶段报告未修改，完整证据保留在修复工作区 `output/pdf/rust-stage18-22-repair/`。
- 新增 `final-audit.json`、`model-input-audit.json`、`wheel-audit.json`、`wheel-{auto,rust}-protocol.json`，以及完整 pytest、Cargo、Clippy、Ruff、rustfmt、构建和安装日志。原暂停目录和日志继续保留，未计入正式结果。

---

以下为暂停时的历史交接记录，验收中的描述已由上述最终状态取代。

## 工作区与源码

- 主修复工作区：`/Users/myhloli/.codex/worktrees/rust-stage18-22-repair/docvortex`，从 `main@efc7bad2070fbfb8a442910ff084917ecef93f66` 创建，当前为 detached HEAD，改动尚未提交。所有后续命令明确以此目录为 `workdir`；不要改动原仓工作区。
- 阶段 20 消融的 Python 包装持有变体：`/Users/myhloli/.codex/worktrees/rust-stage20-wrapper/docvortex`。它与消融时主修复版只在 textpage 调用链不同，两个变体分别独立构建协议 29 扩展。消融已完成，不再用它作为最终候选。
- `v0.5.7@5d2162a` 独立对照：`/Users/myhloli/.codex/worktrees/rust-stage057-baseline/docvortex`，已构建并实际命中 Rust 私有协议 24。最终修复版已重新构建并实际命中协议 29，扩展 SHA-256 为 `a7e86593dc078d50bbe3fe52cad5ca1154453a7e5daeeb41a507cbaac1a24029`。
- 主要报告：[rust-pdf-stage18-22-repair.md](rust-pdf-stage18-22-repair.md)。消融结果已填入，最终性能和完整验证仍标注待完成。完整机器证据在主修复工作区的 `output/pdf/rust-stage18-22-repair/`，该目录被 Git 忽略。
- 用户要求所有新增函数／测试辅助方法有中文注释。已按此执行；后续新增函数继续遵守。Python 环境为 `/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python`（CPython 3.14.4，pypdfium2 5.13.0）。MinerU 源码固定为 `/Users/myhloli/projects/20240809magic_pdf/Magic-PDF@f504cff8b07776760c673023fab8847085158be0`，其原有未跟踪 `examples/` 不要改动。

## 已完成实现

1. 阶段 19：开放四角填充 Path 使用宿主 Python `round(x, 3)`；颜色查询失败保留 `fill_rgba=None`，仅可见性判断把未知 alpha 按 255 处理。真实 PDF 差分检查绘图线、Path 摘要和页面矢量结果。
2. 阶段 21：自有几何证据分别保存原始字号与 run 分组的取整字号；样式阈值仅用原始字号；Unicode 锚点判定独立缓存 `_is_anchor_text` 结果，不由 `script_group` 推断。新增字号 9.9／10.1 和日文中点 `・` 差分。
3. 阶段 22：`Bbox` 快路径先验证内部列表长度、确切 float 类型和有限性，再比较坐标；覆盖 `None` 与可转换字符串。
4. Python 与 Rust 私有协议同步升至 29；旧协议 28 在 `auto` 下回退、显式 `rust` 下报错的测试已加入。
5. 阶段 20 消融后按预定门槛回退页面级 Rust textpage 生命周期迁移：已删除 Rust `read_pdfium_page_text_snapshot` 注册及实现、Python 页面级 bridge，恢复 Python 包装持有 textpage 并调用 `read_text_snapshot(textpage, …)`；保留有效的字符几何物化优化。生命周期测试改为验证 Python 包装在快照后关闭。

## 已完成验证与消融

- 最终回退形态的定向 Rust 后端测试：**264 passed**；Cargo workspace tests、Clippy `-D warnings`、rustfmt、改动文件 Ruff check、`git diff --check` 均通过。此前针对回退调用链的 45 项定向测试也通过。完整 Rust／Python 后端 pytest、全仓 Ruff、ABI3 wheel 独立 Python 3.14 安装验证仍未做。
- 阶段 20 消融：公开 32 PDF 三轮 Rust 持有相对 Python 包装持有改善 `+0.147%、+0.015%、+0.056%`，中位 `+0.056%`；Flash 31 eligible 为 `+0.147%、−0.251%、−0.036%`，中位 `−0.036%`；medium shared 32 PDF 为 `+0.707%、−0.118%、−1.329%`，中位 `−0.118%`。各轮完整输出均一致，没有反序复测仍超过 5% 的单文档退化或超过 130% 的 RSS。没有链路达到“中位改善至少 1% 且三轮均改善”，因此回退。详细逐文档比率、哈希、协议及 RSS 在 `ablation-public/`、`ablation-flash/`、`ablation-shared/` 的 `report.json`。

## 历史暂停点：最终版对 v0.5.7

恢复说明：已重新核对协议 24／29 与扩展 SHA。为避免跨暂停间隔组成一对，`demo4.pdf` 已完成的基线计时和内存目录另存为 `first.before-pause`／`first-memory.before-pause`（对应日志和配置同样另存），整对重新测量。前六份完整配对保留；公开链路三轮验收已恢复。

- 最终公开 parse 三轮配对已启动，**仅第一轮前 6／32 份完成**；这 6 份完整输出均一致，实际扩展协议为基线 24／最终版 29。进度为 `output/pdf/rust-stage18-22-repair/final-public/progress.json`；原日志 `final-public.log` 包含人为 `KeyboardInterrupt` 尾迹，属于暂停操作，不是算法错误。
- 第 7 份 `demo4.pdf` 的基线计时与 RSS 已完成；最终版计时当时尚未生成正式结果。该未完成目录和日志已保留为 `final-public/round-1/06-demo4/second-interrupted/` 与 `second-interrupted.log`，原 `second/` 路径已腾空。配对脚本支持从已完成的计时／内存报告恢复；不要删除已完成证据。
- 运行中的父进程已收到 SIGINT，检查时无 `paired_compare.py`、`rust_pdf.py` 或 `pdf_memory.py` 残留进程。恢复前可再用 `ps` 确认。

恢复公开链路（在主修复工作区运行；日志用追加，保留暂停记录）：

```sh
/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python output/pdf/rust-stage18-22-repair/paired_compare.py \
  --first /Users/myhloli/.codex/worktrees/rust-stage057-baseline/docvortex \
  --second /Users/myhloli/.codex/worktrees/rust-stage18-22-repair/docvortex \
  --suite public --rounds 3 --output output/pdf/rust-stage18-22-repair/final-public \
  >> output/pdf/rust-stage18-22-repair/final-public.log 2>&1
```

之后使用同一命令将 `--suite`／`--output` 分别改为 `flash`／`final-flash`、`shared`／`final-shared`，相应日志名也改为 `final-flash.log`、`final-shared.log`。三条链路勿并行，避免计时互相干扰。`paired_compare.py` 每文档各变体预热一次、热运行五次，逐轮平衡 AB／BA，独立进程树 RSS，超 5% 耗时或 130% RSS 自动反序复测；`shared` 使用同一冻结模型录制。

## 暂停时的剩余任务（现已全部完成）

1. 完成三条最终对照的三轮完整语料；核对每轮总耗时、逐文档比率、进程树 RSS、完整输出哈希和真实协议。若任何完整输出不同，逐字段解释；若某链路有可重复净退化，定位并回退相关优化后重新验收。本轮不以原定 2 倍目标为验收条件。
2. 对最终形态运行 Rust 与 Python 后端的相关及完整 pytest、Cargo tests、Clippy、rustfmt、Ruff，并保留日志和准确计数。定向 264 项和 Cargo／Clippy 已过，源码变化后要按实际需要复测。
3. 构建协议 29 的 ABI3 wheel，在独立 CPython 3.14 环境中安装并证明 `auto`／`rust` 实际命中扩展；核对输出。可参考旧阶段报告中的 wheel 验证方法，但要记录当前 wheel 的真实 SHA 和协议。
4. 将最终配对、差异裁决、完整测试、wheel 验证和剩余风险写入 [rust-pdf-stage18-22-repair.md](rust-pdf-stage18-22-repair.md)，保留原阶段报告为历史。最后检查 `git diff --check` 与工作区状态；当前未提交、未推送、未建 PR、未发布。
