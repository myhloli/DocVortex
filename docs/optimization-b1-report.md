# B1 验收报告：导出路径关系检查

> 完成日期：2026-10-24（Asia/Shanghai）  
> 状态：B1 已验收，下一批为 B2。  
> 对应计划：[分批优化实施计划](optimization-plan-2026-10-23.md)；基准工具与共同基线见 [B0 报告](optimization-b0-report.md)。

## 1. 本批结果

将导出文件之间的两两路径比较改为严格父路径索引。在常见浅层路径下，校验耗时随文件数近似线性增长；一万条路径的中位耗时从 **2.189 秒降至 9.663 毫秒**，降低约 **99.6%**。这项数字只代表路径关系检查，不是整个解析或导出的提速。

- 生产修改仅位于 `src/docvortex/export/middle.py::_validate_export_path_relationships`。
- 新增 `tests/test_export_path_relationships.py`，连同相关既有导出回归，共 **133 passed**。
- 真实写盘覆盖 3 个规模 × 3 个入口 × 3 种模式，共 **27 组**，两个执行方向均输出等价。
- 1,000 个素材的九组场景，合并两轮样本后的导出耗时下降约 **2.7%–21.1%**；小规模场景的整体收益不明显。
- 全部真实写盘测量的最大 RSS 比率约 **1.0192**，未超过 5% 阈值。
- 一个 256 素材场景在初始双轮触发耗时回退门禁，已按第 4 节追加配对定位，保留全部原始记录。
- 13 页真实 PDF 的 Markdown、HTML、bundle、Middle JSON 导出共 **106 个文件全部一致**；完整解析结果、诊断及输入所有权检查通过。

## 2. 实现和语义边界

仍先以原始字符串为键构造 `Path(...).parts`，保留原有去重和首次插入顺序。随后按子文件顺序，为每个严格父路径登记首个子文件；最后按父文件原始顺序查询并报错。

保留以下行为：

- 选择最早出现的冲突父路径，再选择该父路径最早出现的子路径，错误类型和原始路径文字不变。
- 按完整路径段比较，`a` 与 `ab/file.png` 不构成父子关系。
- 规范化后等长的别名、重复输入、空/点路径继续遵循原有词法语义。
- 使用 parts 元组的精确比较，保留 POSIX/Windows 的盘符、UNC、反斜线、大小写和根锚点差异。
- `_resolve_export_target`、`_commit_export_files` 及三个导出入口的调用顺序没有改动；路径逃逸、符号链接、覆盖和异常回滚继续由既有代码处理。

对任意深路径，元组切片与哈希仍有与深度平方相关的成本；本批不宣称任意深度下严格按路径段总数线性。常见 `images/file.png` 输入仅产生少量共享前缀，新增索引开销很小。

## 3. 纯路径规模测量

复用 B0 冻结驱动中的 `precheck()`，两个独立源码目录按 AB/BA 执行；每轮每版本计时五次，共十次。下表为合并样本的中位数，三个规模的输入摘要逐一相同。

| 总路径数 | 参考实现 | B1 候选 | 候选 / 参考 |
| --- | ---: | ---: | ---: |
| 1,000 | 22.833 ms | 0.922 ms | 0.04038 |
| 3,000 | 192.974 ms | 2.813 ms | 0.01458 |
| 10,000 | 2,189.452 ms | 9.663 ms | 0.00441 |

这些任务只有词法路径检查，不包含写盘、序列化、解析或素材处理。证据为 `path-scale-report.json`、`path-scale/` 与 `path-scale-commands.json`。

## 4. 真实写盘与回退定位

使用未改动的 B0 冻结 `export_files.py`，配置如下：

- 素材数：8 / 256 / 1,000；每份 PNG 为 4,096 字节。总文件数还包含主文件或 bundle manifest。
- 入口：`write_artifact`、`save_bundle`、`export_middle_json`。
- 模式：首次写出、同内容复用、不同内容覆盖。
- 每个场景五次正式计时，阶段诊断和独立 RSS 另行运行。
- 整组先 reference → candidate，再 candidate → reference；每次使用新输出目录。
- 所有主文件、素材、引用和输入摘要通过；同内容模式验证已有文件未被重写。

| 素材数 | 九个场景的合并耗时比范围 | 场景比率中位数 | 最大 RSS 比率 |
| --- | ---: | ---: | ---: |
| 8 | 0.9185–1.0495 | 1.0119 | 1.0049 |
| 256 | 0.8697–1.1014 | 0.9862 | 1.0192 |
| 1,000 | 0.7895–0.9725 | 0.9349 | 1.0143 |

这里的场景比率中位数不是总运行时间比率。原始表和所有逐次样本保存在 `export-summary.json` 与 `export-forward/`、`export-reverse/`。

### 256 素材的 save_bundle 同内容复用

该场景初始两轮候选 / 参考分别为 **1.0765、1.0770**，触发持续回退门禁。原始正式计时存在重叠，额外阶段诊断中路径检查已由约 1.6 ms 降到 0.26–0.32 ms；其他文件路径检查和读取时间有波动，单次诊断不足以确定原因。

没有修改生产代码或替换基线，追加以下定位：

| 定位条件 | 配对次数 | 每进程计时次数 | 配对中位比率 | 最大单对比率 |
| --- | ---: | ---: | ---: | ---: |
| 相同 live 绝对路径，`PYTHONHASHSEED=0` | 6 对，AB/BA 各三对 | 15 | 0.9068 | 1.0131 |
| 相同 live 绝对路径，默认随机 hash seed | 4 对，AB/BA 各两对 | 15 | 0.9320 | 0.9405 |

每个进程先取本轮中位数，再比较配对结果；不能将同一进程的 15 次调用当作 15 个独立进程样本。每轮仍会删除并复制目标目录，因此控制的是路径，不是 inode 或完全相同的缓存状态。归档、准备和校验均在正式计时外。

十对专项测量均未再次出现超过 5% 的回退，且完整输出一致。本批据此完成该场景的验收，但不将定位结果表述为已经证明最初波动的唯一原因。初始回退标记原样保留在 `export-summary.json`；专项结果位于 `bundle-same-controlled/`、`bundle-same-random-seed/`，最终裁决单列在 `acceptance.json`。

## 5. 真实 PDF 与聚焦回归

使用 B0 冻结的 `demo/pdfs/demo1.pdf`，两个源码分别独立解析一次，保留 ModelJson，再从同一结果导出：

1. Markdown 与 HTML；
2. 带原始模型的 bundle，并实际回读；
3. 从保留的 ModelJson 重建尚未物化图片的 MiddleJson，再执行 `export_middle_json`。

最后一步不能直接传 `parse()` 返回的已物化 MiddleJson，因为旧导出入口没有 `assets` 参数。带模型的 bundle 可能增加按摘要命名的素材引用，回读时验证全部原始素材保留，再比较两个版本的完整文件集合。

实际结果：**13 页、20 份解析素材、106 个导出文件**。全部文件的相对路径、大小和 SHA256 相等；完整解析结果及 diagnostics 相等，导出前后输入不变。证据为 `real-pdf-confirmed/comparison.json` 和两个版本的 `report.json`。

首次外部验收脚本缺少 multiprocessing 主入口保护，导致 PDF 裁图子进程重复执行主流程。该脚本已修正，并在新的 `real-pdf-confirmed/` 目录重跑通过；初始脚本和 `real-pdf-reference.log` 保留。该失败发生在未修改的参考源码上，没有通过改动生产逻辑处理。

聚焦回归命令：

```sh
PYTHONPATH=src /Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python -m pytest -q \
  tests/test_export_path_relationships.py \
  tests/unittest/test_middle_json_export.py \
  tests/test_export_image_names.py \
  tests/test_export_html_images.py \
  tests/test_public_export_api.py \
  tests/test_document_envelope.py
```

结果：**133 passed in 1.05s**。本批两个 Python 文件的 Ruff 检查、格式检查及 `git diff --check` 通过。没有新增时间阈值单测。

## 6. 身份、证据与交付边界

证据根目录：

`/Users/myhloli/Documents/Codex/docvortex-optimization-20261023/B01`

| 项目 | 值 |
| --- | --- |
| 基线提交 | `b2ef8d025aed2b1041da6ec6e766e9d317f716ce` |
| 参考源码 | `B00/reference-source`；上一已验收批 B0 没有生产改动，因此也是共同生产基线 |
| 候选源码 | `B01/candidate-source`，共同基线仅叠加本批 `export/middle.py` 修改 |
| 参考源码/资源 SHA256 | `802293be092ca7e36f2d63ef84df381933e26307b67f7d904b1eb24f9c7afc87` |
| 候选源码/资源 SHA256 | `093fac5e93f5ec71ebcdb8a12f6b0c1630308b075ac2db959de8082e052b1343` |
| 原生扩展 SHA256 | 两边均为 `b75a91e149cc1c7d6101ee7a186b3aa1213499403a5a9a53ef7a5edb976c4c9f`；Rust 源码未变，复用匹配的 B0 构建 |
| B0 驱动清单 SHA256 | `9008a9b0495f840a766f36087cf4430408ba4e0ed88d6944c94ea5f2495a325e`，没有更改 |
| 实现补丁 SHA256 | `b942efa2506c7d1d3ce0f55d14fff9f1f5deebaf5b9bc9c302106e82a853b0bf` |

`identity.json` 记录参考、候选和工作树身份；`implementation.patch` 只包含本批生产改动；`change-manifest.json` 冻结实现与新增测试文件。`acceptance.json` 汇总验证及初始异常的裁决，原始测量报告没有被覆盖。

已有 B0 工具改动和四个 recheck 文件均保持原样。本批未改动渲染、解析、导出事务、依赖或版本；尚未执行 B2，**未提交、未推送、未合并、未发布**。
