# 2026-09-08 文本边界规则金标准复核

旧实现基线：c2462ae。移除 fast-langdetect 后，逐边界判断空格。

- 同环境比较真实 PDF 的原生中间结果，类型、结构、顺序、几何和非文本字段全部不变。
- 中文论文的变化为西文词间空格、CJK 边界空格及 Voting / Stacking 等物理断词恢复；保留 GLGE-difficult、Open-domain、chain-of-thought 及 multi-level。
- 中文论文逐页 fingerprint 和 bbox_fingerprint 均包含文本，因此连同文本摘要同步更新；原有块 ID 和几何摘要保持不变。
- engineering_process_restrictions_table.pdf 第 1 页第 1 表仅空格变化，行列和合并单元格签名不变；更新文本哈希，不提升原 legacy_unverified 的准确性声明。
- 原稀疏表已知偏差仍保持独立诊断，不纳入本次金标准调整。
