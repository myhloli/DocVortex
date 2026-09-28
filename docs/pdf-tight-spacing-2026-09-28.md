# 非 CJK 原生文本词距修复（2026-09-28）

## 行为与边界

相邻普通非 CJK 字符在同一方向和基线上、源编号连续、字号与页面墨迹尺度可信时，若 tight bbox 间隙严格大于两侧较大字号的 0.25 倍，则补一个 ASCII 空格。任意一侧为 CJK、标点或符号时不新增词界；已有空白及原有补空格规则保持不变。没有行长或样本数量门槛，不使用字距统计或词典。

完整语料实际发现的反例决定了三个保守限制：

- 部分 PDF 用 1pt 字号配合文字矩阵放大；墨迹高度超过自身字号 1.5 倍时不进行新增推断。
- 连续数字中的窄字形（例如 Arial 的 `11`）有自然大留白，数字内部不新增空格。
- 等宽字体中的 `i` 等窄字形不能仅靠墨迹留白判词界，固定宽度标志或通用等宽字体元数据命中时跳过。

同时排除显著字号变化、上下标位移、不可靠方向和跨行几何。这些边界有意牺牲召回率，避免误拆正常文本。

## 接入与兼容

- Flash 从视觉行建立紧凑词界证据，在自然语言块归属和分类完成后、链接及样式物化前应用；代码、公式与原始字符不改写。复用现有去行末连字符的对齐规则。
- Rust 快照直接批量返回源词界编号；Python 回退使用同一判据。不会为补空格额外持有整页字符副本。
- MinerU Basic/Standard/Advanced 的 TXT 字符回填、Rust span 内容物化和旋转 line/span 回填均接入。实际代码区域的禁用标记会穿过整页虚拟文本框。
- Flash/Basic/Standard 的原生表格在单元格内部物化空格；表格恢复失败后的文本投影也接入。通用代码空间投影、OCR 和模型内容保持原行为。
- 公共 parse 参数及 MiddleJson schema 不变。新增 Rust span 参数可省略，旧宿主省略参数时保持原行为；内部 native protocol 升至 23，防止混用旧扩展。
- DocVortex 本地版本为 0.5.4；MinerU 最低依赖同步为 `docvortex>=0.5.4,<1`。本次未提交、推送或发布。

## 质量验证

输入 `demo/pdfs/中文论文2.pdf`，SHA256 为 `937cfb51e9e8abc6b3a2d8c46d38003cd2ad795a0692eab3f9e92ac604fcc9ad`。

完整重放 41 份 PDF、457 页：40 份输出完全不变。目标论文的 21 页、187 个块新增 2,095 个空格；非空格字符、块数量、类型、顺序和坐标保持不变。原件全部 23 页已打开审阅，包括两个无变化页；第 2 页表格的 `ChatbotArena` 修复为 `Chatbot Arena`。

Python/Rust 的 41 份完整 ModelJson/MiddleJson 输出全部一致。四档均通过公共 `mineru.parser.parse()` 完整解析 23 页并导出 ModelJson、MiddleJson、Markdown、HTML 和素材；Basic/Standard/Advanced 的原生内容调用计数证明实际进入 Rust 路径。四档均恢复 `Large Language Models`、`Natural Language Processing`、`School of Computer Science`、`Pre-training of deep bidirectional transformers` 和参考文献会议名称，且不再出现 `LargeLanguageModels`。四档 HTML 已在本地浏览器检查。

测试日志保存在本次运行目录：

- `docvortex-final.log`：2,593 项通过，3 条既有 Office 依赖警告。
- `mineru-final.log`：172 项通过。
- `python-final.log`：68 项通过，3 个仅用于 Rust 的测试按预期跳过。
- `final-focused.log`：220 项通过。
- `final-boundaries-02.log`：128 项通过，含代码空间投影隔离。
- `rust-final.log`：15 项 Rust 测试通过。
- Ruff 与两个仓库的 `git diff --check` 通过。

仅更新 `flash_reviewed_history.json` 中目标论文的 21 页内容指纹，以及 `native_pdf_table_demo_manifest.json` 中一处单元格文字和对应 HTML 指纹。逐页裁决见 `tests/fixtures/pdf_tight_spacing_review_decisions.json`；其余基线保留。

## 性能与证据

冻结旧源码和扩展，在没有模型推理并发的阶段，以独立进程逐份交错测量基线与候选，各预热后计时 3 次。41 份语料中位耗时之和增加约 1.31%；目标论文由 1.6792s 到 1.7251s（+2.74%），RSS +0.14%。峰值 RSS 最大增幅约 0.80%。

一份约 42ms 的短文档首次测得 +9.90%；反转先后顺序、各测 7 次后为 41.656ms → 43.727ms（+4.97%），没有复现持续超过 5% 的退化。模型档位单次全文耗时受模型缓存和推理影响，不用来证明原生规则性能。

完整日志、源码快照、逐页对比和四档输出位于 `output/pdf/tight-spacing-20260928/`。主要证据：`final-tiers/`、`final-corpus-diff.json`、`review-final/`、`python-corpus/report.json`、`performance/report.json`、`performance-retest/report.json`。拒绝的早期候选保留，未作为最终输出交付。
