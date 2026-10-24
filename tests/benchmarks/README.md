# Flash PDF 等价性基准

## 环境、身份与恢复

使用已有 DocVortex 开发环境，并安装基准额外依赖：

```bash
python -m pip install psutil
```

`psutil` 只用于基准采样，不是新增的产品运行依赖。运行前确认实际 Python、`docvortex.__file__` 和
`backend_info()`；冻结源码目录必须含与其源码匹配的原生扩展。`PYTHONPATH` 不会构建扩展。
基准不能与构建、全量测试或其他重负载任务同时运行。

公开解析的完整输出定义统一在 `rust_pdf.public_output()`：包含 ModelJson、MiddleJson、全部素材路径与
字节摘要，以及完整诊断。源码身份包含 Python/Rust 代码和包内资源；扩展路径、SHA256、协议单独校验。
解释器、虚拟环境、平台和运行依赖版本也参与恢复校验。

当前恢复入口拒绝缺少完整身份字段的历史记录，或输入、区域、源码、扩展、环境、采样次数不一致的记录。
保留旧报告并使用新输出目录重测，不通过手工补摘要或覆盖旧报告绕过校验。性能比较期间不要 bump 产品版本，
因为完整输出中的 producer 版本也是契约的一部分。

## 两个原生版本的公开/共享接口比较

```bash
python tests/benchmarks/compare_pdf_revisions.py \
  --reference-source /absolute/path/to/reference \
  --candidate-source /absolute/path/to/candidate \
  --suite public --corpus all --runs 5 \
  --output output/optimization/public
```

该入口按文档交替版本顺序，每个版本预热后计时，独立进程重放 RSS；持续超过 5% 的回退需要反序复测。
保存的完整输出不等价或持续回退时非零退出。恢复时重新核对当前输入、源码、扩展、环境和保存的输出产物。
`--path` 可重复指定子集，`--extra-path` 可重复添加默认集合外的样本。Form 优化需额外覆盖：

- `tests/unittest/pdfs/form_containers/research-report.pdf`
- `tests/unittest/pdfs/form_containers/2022.emnlp-main.614.pdf`

`--suite shared` 还需 `--flash-baseline`，且冻结区域输入必须涵盖全部所选 PDF；缺失时中止。
`flash_pdf.py --runs 0` 可生成冻结区域，但该脚本的 `--path` 会替换默认集合，且没有 `--extra-path` 参数。

独立复核已有配对报告的进程树 RSS：

```bash
python tests/benchmarks/pdf_memory.py \
  --timing-report output/optimization/public/report.json \
  --output output/optimization/memory-audit
```

当前比较标签仅为 `rust-reference` 和 `rust-current`。相同比较定义用于比例计算及超过 5% 后的反序复测。
shared 报告复核需传入同一 `--flash-baseline`。空报告、缺标签、无有效采样或恢复身份变化均失败。

## AB/BA 多轮配对入口

```bash
python tools/benchmark_native_pair.py \
  --baseline-root /absolute/path/to/reference \
  --candidate-root /absolute/path/to/candidate \
  --input-dir /absolute/path/to/frozen-pdfs \
  --rounds 5 --output output/optimization/paired
```

输入目录只采集直接子文件中的 `*.pdf`，不递归且不支持 XOR 包装；实际集合和每份字节摘要在开始时冻结。
通过 `--python` 指定目标解释器。每轮保存完整输出及 worker 日志，同时检查版本内部稳定性和版本之间等价性，
所有门禁通过后才写性能比例；失败报告标记 `status=failed`，完整证据保留在原目录。

解析时间复用 `public_once()` 的边界，完整输出序列化不进入计时。RSS 改为每轮每文档独立进程重放、采样结束后
才序列化，并取该轮各文档峰值的最大值；汇总取各轮中位数。**此口径与旧版整份语料同进程 RSS 不可直接比较。**
需要逐文档回退门禁时同时使用上一节的 `compare_pdf_revisions.py`，不能只看配对总耗时。

## 文件导出专用基准

```bash
python tests/benchmarks/export_files.py \
  --source-root /absolute/path/to/reference \
  --files 8 64 256 --payload-bytes 4096 --runs 5 \
  --output output/optimization/export-reference
python tests/benchmarks/export_files.py \
  --source-root /absolute/path/to/candidate \
  --files 8 64 256 --payload-bytes 4096 --runs 5 \
  --baseline output/optimization/export-reference \
  --output output/optimization/export-candidate
```

`export_files.py` 覆盖 `write_artifact`、`save_bundle`、`export_middle_json` 三个入口的真实写盘，分别测试
首次写出、同内容复用、不同内容覆盖。`--files` 是素材数，不含主文件或 manifest；PNG 素材具有确定内容和指定字节数。
比较时要求同一工作负载、输入和完整文件输出，逐项校验引用、bundle 回读和输入未被污染；同内容模式还检查文件未被重写。

正式总时间包含实际 API 写出，不含输入构建、旧文件准备、输出验证、阶段包装或 RSS 采样。物化、路径关系检查、
路径解析和提交阶段使用额外诊断轮分账，不能把这些包装时间当作正式加速结果。独立进程采样 RSS；50 ms 采样及端点观测
仍可能漏掉更短的峰值。测试使用预热后的系统缓存，不执行 `fsync`，不声称测量冷盘吞吐或断电一致性。

纯路径预检另有规模计时，但不能替代实际写出验证。较大规模会生成真实文件，应先检查输出卷可用空间。
这套工具提供输出等价性与性能比例；超过 5% 的变化仍须按优化计划单独反序复测，不能仅凭一次比例作结论。

## PDF 导出性能与等价性

`pdf_render.py` 只测量已物化 MiddleJson 到 PDF artifact 的导出，不包含 Bundle 读取、PDF 解析、视觉检查和写盘。
每个输入及 ORIGINAL/REFLOW 组合使用独立进程，首次调用单列，随后默认计时五次取中位数。

```bash
python tests/benchmarks/pdf_render.py --source /path/to/bundle --source fixture:rich --backend python --output output/pdf/performance/baseline
python tests/benchmarks/pdf_render.py --source /path/to/bundle --source fixture:rich --backend accel --output output/pdf/performance/accel --baseline output/pdf/performance/baseline
```

`--source` 可重复指定 Bundle；不指定时使用含中英混排、矢量公式、链接和重复图片的合成语料。
`--backend python` 在独立测试进程中禁用 ReportLab `_rl_accel`，`accel` 则要求实际使用该加速模块；
这是 ReportLab 的选项，不会关闭 DocVortex 必需的 Rust 后端，也不改变其他进程的 ReportLab 配置。
`--runs 0` 只运行首次导出与等价性检查。`--profile` 额外保存 cProfile，剖析不参与耗时和内存测量。
RSS 每 50 ms 采样，包含解释器和已加载输入，不是文档增量；非常短的峰值可能未被采到。

报告冻结输入/素材摘要、依赖版本、Git 状态、实际加速后端、PDF 字节、诊断、逐页像素/文字/链接和页面尺寸。
同后端基线要求字节一致；跨后端仍严格比较视觉与诊断。输出目录不能覆盖，输入集合不一致或结果不等价时报错。
首次调用包含惰性渲染模块加载，但不包含解释器启动及 Bundle 读取。性能测量期间不要并行运行测试或其它重负载任务。

## Flash 原生分析

在仓库根目录执行，默认合并版本化布局语料、全部 demo PDF 和原生表格文档，实际集合以当次输入清单为准：

```bash
python tests/benchmarks/flash_pdf.py --output output/flash_pdf_refactor/baseline
python tests/benchmarks/flash_pdf.py --output output/flash_pdf_refactor/candidate --baseline output/flash_pdf_refactor/baseline
```

每份文档在独立子进程中预热一次、计时五次，报告中位耗时与进程峰值 RSS。耗时包含 PDFDocument 打开、原生分析和关闭，不包含后处理、输出序列化、OCR 或渲染。RSS 在输出后处理和额外剖析之前采集；它包含解释器和已导入模块，不是文档的增量内存。

每份文档保存完整 `model_list` 和通过统一后处理入口生成的 `middle_json`。仅调用 DocVortex 原生流程，版本字符串固定，比较不移除任何输出字段。`report.json` 包含源码版本、依赖版本、文件指纹与历史清单差异；历史清单不会被改写。`comparison.json` 分别报告完整输出是否相等、耗时比例和 RSS 比例，输出不等价时命令失败。

使用 `--runs 0` 跳过预热和重复计时，只进行功能比较。`--path` 可重复指定固定子集；基线和候选必须使用同一份语料集合。`--profile` 额外生成逐函数调用次数、自身耗时与累计耗时，可定位提取、几何校准、页面准备、页面组装和样式物化阶段；剖析耗时不参与性能比较。性能测量期间避免同时执行测试、构建或其它 CPU 密集任务。

已有报告目录不能覆盖。先在原始版本建立基线，然后在候选版本使用新的输出目录；不可通过更新基线掩盖输出变化。超过 5% 的持续耗时或 RSS 回退应独立复测并定位。
