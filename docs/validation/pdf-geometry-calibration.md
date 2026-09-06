# Droid 几何参数标定与规则消融

本轮保留的改动：删除 `_consecutive_source_row()` 中名义字号比不得超过 `1.2`
的限制及相应字体字段遍历。原始框已经通过文本矩阵变换，字号仍是 PDF 文本状态中的
名义值；8pt 和经 0.5 倍缩放的 16pt 文字可以具有相同的显示大小。
同行恢复继续检查原始框、基线、源字符连续性、方向、保留的拆分边界和表格隔断。

没有修改 X/Y 全局阈值、tight padding、字体策略、公开接口或原有金标。
正文公式恢复分支继续保留。本轮不更新发行包。

## 对照方法

- Python 3.13.5、pypdfium2 5.10.1、Pydantic 2.12.5、NumPy 2.2.6；固定 `droid-cjk-v1` 字体。
- 在 `b4afb58` 独立工作树上运行 23 组配置 × 3 份初筛 PDF，合计 69 次。
- 冻结完整 31 份 PDF 的公开 ModelJson 与 MiddleJson，并对基线和三个候选运行完整对照。
- 在第二个固定版本 `be494c6` 上再次比较基线与删除字号门槛后的 31 份输出。
- 原生语义金标在公共字符归一化之前检查；完整输出摘要在归一化及后处理后检查。
  两个阶段不能混用，否则会把已有符号规范化误报为阈值回归。

实验期间主工作区另有上下标修复提交。早期主工作区的运行不作为最终证据；曾观察到的
作者名 g/p 样式差异也不能归因给删除字号门槛。下面的结论全部来自固定工作树。
机器可读参数、源码指纹、输入/输出 SHA256、逐文档差异见
[完整实验记录](pdf-geometry-calibration.json)。原始大体积输出保存在本地
`.baseline/droid-calibration/pinned-*` 和 `current-*` 目录中。

## 结果

| 配置 | 初筛结果 / 全语料结果 | 决定 |
| --- | --- | --- |
| 删除名义字号比 1.2 门槛 | 两个固定版本分别 31/31 完整输出不变 | 保留简化 |
| 删除源字符同行恢复分支 | 中文论文4有 7 条原语义断言失败 | 保留分支 |
| 删除分片正文公式恢复 | 中文论文4有 3 条原语义断言失败 | 保留分支 |
| X 阈值组从 1.15 到 2.2 扫描，同时删除同行恢复 | 未通过中文论文4；2.0 组改变 2/31 份输出，并产生语义失败 | 不采用 |
| Y 风险阈值改为 1.8 / 2.6 / 3.0，同时删除恢复分支 | 均未修复目标样例 | 不采用 |
| 同行最大间距改为 1.5 / 3 / 6 / 10.5pt，同时删除同行恢复 | 10.5pt 虽恢复部分图注，但改变 9/31 份输出，目标样例仍有语义失败 | 不采用 |
| tight padding 0.5 / 1.5pt，或将 X 修复左边界限制到 origin | 未能替代恢复分支 | 不采用 |
| 移除基线 / 源索引门槛 | 改变英文公式对照文档；它们仍承担结构保护 | 保留门槛 |
| 将原名义字号比改用现有 canonical 尺度比较 | 仍有目标语义失败 | 不采用 |

上述 2/31、9/31 是完整输出变化数量，不把每处变化都自动判定为错误；候选被拒绝的
直接原因是仍有语义失败，且无法证明能安全替代现有恢复逻辑。

中文论文4触发 X 修复的主要是 DY3/DY5 已嵌入西文字体的数字/字母 run，宽度与
origin advance 的中位比例约 1.83–2.03；这不是 Droid 的统一侧边距偏移。
因此不宜通过抬高整个 X 异常阈值来补偿 CJK 字体变化。

## 验证

- 新增真实 PDF 用例：8pt 的 A 与文本矩阵缩放为 0.5 的 16pt B，名义字号不同，
  原始框高度和基线相同。旧实现拒绝同行恢复，简化后通过；基线错开的负例继续拒绝。
- 新用例及既有同行/分式保护测试：3 passed。
- 在 `be494c6` 固定快照应用最终改动后：438 passed、1 deselected。
- deselect 的仍是已有 `test_demo_sparse_table_confidence_manifest`，未改变其金标。
- 所有大语料对照中，保留改动的字符、样式、几何、素材载荷与中间结果摘要均保持一致。

## 复现

门槛消融替身对应 b4afb58 的实现，工具会核对 `line_merging.py` 的源码指纹，防止在
门槛已变化的实现上产生假的单因素对照；运行结束还会核对整个包的源码是否变化。
实验替换只存在于工具进程中，不进入解析器运行时配置。

```bash
git worktree add --detach .baseline/geometry-reference b4afb58
cp tools/calibrate_pdf_geometry.py .baseline/geometry-reference/tools/
cd .baseline/geometry-reference
uv venv --python 3.13
uv pip install '.[test]' 'pypdfium2==5.10.1' 'pydantic==2.12.5' 'numpy==2.2.6'
uv run --no-project python tests/benchmarks/flash_pdf.py --output .baseline/reference --runs 0
uv run --no-project python tools/calibrate_pdf_geometry.py --corpus screen --output .baseline/screen
uv run --no-project python tools/calibrate_pdf_geometry.py --corpus full --variants baseline no_size_gate gap_10.5_no_source x_2.0_no_source --baseline .baseline/reference --output .baseline/full
```

耗时仅作为运行记录，不作为本轮性能提升结论。当前结果支持删除一处冗余门槛，
不构成“其余阈值已全局最优”或“所有恢复分支都能通过调参删除”的结论。
