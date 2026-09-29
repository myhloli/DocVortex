# Rust 阶段 18～22 行为修复与性能复验

## 范围与基线

本轮从 `main@efc7bad` 出发，修复阶段 19、21、22 已复现的五处 Python/Rust 差异；单独消融阶段 20 的 textpage 生命周期迁移，并与 `v0.5.7@5d2162a` 重新做完整语料配对对照。原阶段报告保留为历史记录，原定 2 倍目标不作为本轮验收条件。

## 行为修复

- 开放四点填充 Path 的角点按宿主 `round(x, 3)` 判断，修复临界坐标下 Rust 漏掉绘图线的问题。颜色查询失败时，Path 摘要保留 `fill_rgba=None`；可见性仍按未知 alpha 为 255 判断。
- 自有几何证据分别保留 PDFium 原始字号与字体 run 分组所用的四分之一取整字号。样式风险阈值只消费原始字号；锚点由参考实现的 Unicode 字母／数字、可打印且非空白规则独立判断，不能由 `script_group` 推导。
- `Bbox` 快路径先校验内部列表长度、元素确切类型和有限性，再比较坐标；不满足条件的输入继续走通用转换。
- Python 与 Rust 私有协议同步升至 29。旧协议 28 在 `auto` 下回退，在显式 `rust` 下报告扩展不兼容。公开 SDK、输出 schema 与后端选择保持原状。

回归差分覆盖开放 Path 的舍入边界、颜色查询失败、字号 9.9/10.1 的双向样式风险、日文中点 `・` 的布局锚点以及含 `None`、可转换字符串的 `Bbox`。定向 Rust 测试与 Cargo workspace tests、Clippy、rustfmt、Ruff 检查已通过；完整验收状态和计数见下文。

| 已复现差异 | 回归输入与修复后的行为 |
| --- | --- |
| 开放 Path 舍入 | 角点 `20.0625` 与 `20.062498` 使用宿主三位舍入后归入同一 x 坐标；绘图线、Path 信息和页面矢量结果逐字段一致。 |
| 填充颜色失败 | 以真实 C ABI 回调返回失败；仍判为可见，`fill_rgba=None` 与参考一致，下游不误认为存在可见非白填充。 |
| 字号阈值 | 字号 `9.9`／行高 `14.925` 应有样式风险，字号 `10.1`／行高 `15.075` 应无风险；两页差分覆盖取整导致的漏报和误报。 |
| 日文中点锚点 | 六个真实 PDF 字符 `・` 归入 cjk 组，但不是 Unicode 字母／数字；布局风险与参考一致。 |
| Bbox 异常类型 | 内部含 `None` 时走通用转换并返回无效结果，可转换字符串坐标按原逻辑转换，均不在快路径比较时抛出类型错误。 |

## 阶段 20 消融

两个独立构建使用同一修复源码与协议 29；只切换调用链中 textpage 由 Rust 加载／关闭，还是由 Python 包装持有。32 份公开 PDF、31 份 eligible Flash 和 32 份冻结 medium shared 样本分别进行三轮配对；每轮逐文档平衡 AB／BA，每个变体独立预热一次、热运行五次。进程树 RSS 在独立进程采样，完整输出逐样本核对。只有某链路总耗时中位改善至少 1%、该链路三轮均改善，且无反序复测仍超过 5% 的单文档退化或超过 130% 的资源占用，才保留 Rust 生命周期迁移。

总耗时以每文档五次热运行的中位数求和，表中“改善”均为 Rust 持有相对 Python 包装持有的变化：

| 链路 | 轮次 | Python 包装持有 | Rust 持有 | 改善 | 输出一致 |
| --- | ---: | ---: | ---: | ---: | :---: |
| 公开 parse，32 PDF | 1 | 13.895563 s | 13.875193 s | +0.147% | 是 |
| | 2 | 13.922553 s | 13.920490 s | +0.015% | 是 |
| | 3 | 14.072387 s | 14.064450 s | +0.056% | 是 |
| Flash，31 eligible | 1 | 13.909381 s | 13.888866 s | +0.147% | 是 |
| | 2 | 13.823132 s | 13.857865 s | −0.251% | 是 |
| | 3 | 13.813139 s | 13.818152 s | −0.036% | 是 |
| medium shared，32 PDF | 1 | 14.614840 s | 14.511583 s | +0.707% | 是 |
| | 2 | 14.539901 s | 14.557028 s | −0.118% | 是 |
| | 3 | 14.692728 s | 14.887990 s | −1.329% | 是 |

三条链路改善中位数依次为 **+0.056%、−0.036%、−0.118%**，没有一条满足预设 1% 与三轮同向的保留条件。初测超过 5% 的样本均执行反序复测，没有持续超限：公开 `demo4.pdf` 为 `1.1301 → 0.9826`；Flash 两份分别为 `1.0552 → 1.0227`、`1.0600 → 0.9931`；shared 三份分别为 `1.0887 → 1.0078`、`1.0986 → 0.9846`、`1.0644 → 0.9814`。三条链路最大 RSS 比率为 `1.1599`、`1.0029`、`1.1211`，均低于 130%。两构建均实际命中协议 29，逐文档完整结果一致。依照预先确定的规则，**移除页面级 Rust 加载入口并恢复 `read_text_snapshot(textpage, …)` 调用链**；保留字符几何物化改进。

## 最终版本相对 v0.5.7

两版使用同一 CPython 3.14、PDFium、MinerU 源码和冻结模型录制；三条链路均逐文档双向配对，分别预热一次、热运行五次，RSS 独立进程树采样。逐轮总耗时、逐文档比率、完整输出哈希、实际扩展协议与源指纹保存在证据目录。完整输出差异须逐项解释，不因速度改善而放行。

公开 parse 比较 model、middle、诊断和全部素材摘要；Flash／shared 比较完整业务返回值，仅去除每次运行必变的 `elapsed` 计时字段。文本、字号、坐标、页面、模型结果及素材字段均不忽略。shared 只回放模型调用边界，分类、栅格化、文本与表格证据、素材及页面几何仍执行真实入口。

三条链路均已完成三轮，完整输出均一致。总耗时采用逐文档热运行中位数之和：

| 链路 | 轮次 | v0.5.7 | 最终修复版 | 改善 | 输出一致 |
| --- | ---: | ---: | ---: | ---: | :---: |
| 公开 parse，32 PDF | 1 | 16.104086 s | 14.589224 s | +9.407% | 是 |
| | 2 | 15.412777 s | 13.911730 s | +9.739% | 是 |
| | 3 | 15.722997 s | 14.233506 s | +9.473% | 是 |
| Flash，31 eligible | 1 | 15.547422 s | 13.892804 s | +10.642% | 是 |
| | 2 | 15.408338 s | 13.835638 s | +10.207% | 是 |
| | 3 | 15.410612 s | 13.867554 s | +10.013% | 是 |
| medium shared，32 PDF | 1 | 15.997826 s | 15.180352 s | +5.110% | 是 |
| | 2 | 15.859730 s | 15.246385 s | +3.867% | 是 |
| | 3 | 15.955903 s | 15.259034 s | +4.367% | 是 |

公开链路改善中位数为 **9.473%**，96 对输出全部一致，最高 RSS 比率为 `1.044127`；Flash 为 **10.207%**，93 对 eligible 输出全部一致，最高 RSS 比率为 `1.043778`；shared 为 **4.367%**，96 对输出全部一致，最高 RSS 比率为 `1.024588`。公开与 Flash 未触发反序复测；shared 的 `flash_table_annotations_synthetic.pdf` 第二轮初测 `1.119975`，反序为 `0.880830`，第三轮初测 `1.061502`，反序为 `1.022380`，均未形成持续退化，复测输出也一致。三个链路的三轮总耗时均改善，资源均低于 130%，没有可重复净退化，因此不追加回退。

`small_ocr.pdf` 在两版 Flash 中均分类为 OCR，每轮排除，31 份 eligible 集合稳定；在 medium shared 中正常测量。285 对正式配对及 2 对反序复测业务输出全部一致，没有需要解释或豁免的业务字段差异。实际命中协议与源码／扩展指纹跨文档、跨三条链路和三轮均保持稳定，见 `final-audit.json`。

计时之外，32 份 shared 文档在基线与修复版各执行一次独立严格输入审计（一次预热、一次检查调用），验证每个模型事件的数组／图像像素、参数类型、标量及顺序与冻结录制一致，且消费全部事件。**64 个构建／文档审计全部通过**，输出同时与正式计时产物一致；这些带输入哈希的运行明确标为 `timing_eligible=False`，不计入性能数字，见 `model-input-audit.json`。

## 测试与 wheel 验证

| 验证 | 结果 |
| --- | --- |
| 源码 Rust 相关回归 | 264 passed |
| 源码 Python 相关回归 | 120 passed / 144 skipped（Rust 专属差分与接口用例不在 Python 模式运行） |
| Rust/session 完整 pytest，4 workers | 5479 passed / 14 skipped，46.87 s |
| Python/legacy 完整 pytest，4 workers | 4765 passed / 728 skipped，81.73 s |
| Cargo workspace tests，`--locked` | 15 passed，0 failed |
| Clippy，`--workspace --all-targets -- -D warnings` | 通过 |
| rustfmt，`--all --check` | 通过 |
| Ruff check 全仓及五份证据脚本 | 通过 |
| Ruff format check，全部 src 与五份修改测试 | 406 files already formatted |
| 独立安装 wheel 的相关回归 | 264 passed，5.19 s |
| `git diff --check` | 通过 |

完整 pytest 没有 deselect 用例，包括 CI 单列诊断的少线表 manifest 测试。两种后端各有 6 个 WMF／Pillow 相关警告，均无失败。Rust 完整测试的 14 个跳过与阶段 22 历史验收计数相同；本轮新增的 Rust 行为差分全部执行通过。

构建产物为 `docvortex-0.5.7-cp310-abi3-macosx_11_0_arm64.whl`，实际 WHEEL tag 为 `cp310-abi3-macosx_11_0_arm64`，wheel SHA-256 为 `63f5327fb7f4b3c835923644fd750347eea7c161264092049784016925cb9d6c`。wheel 内扩展 SHA 与最终性能测试扩展完全相同：`a7e86593dc078d50bbe3fe52cad5ca1154453a7e5daeeb41a507cbaac1a24029`。

在新建 CPython 3.14.4 venv 中安装 wheel 及完整依赖，清除 `PYTHONPATH`，不引入 `.venv4` 的 site-packages；包和扩展均实际从该 venv 加载。`demo1.pdf` 的 Python／Rust 完整解析输出一致，`auto`／显式 `rust` 均实际命中协议 **29**，各记录 13 次文本快照命中，两者输出一致，且页面级 Rust textpage 加载接口已不存在。安装环境另执行本轮相关回归 264 项全部通过，详见 `wheel-audit.json`、`wheel-auto-protocol.json`、`wheel-rust-protocol.json` 与 `wheel-regression-tests.log`。在线依赖安装的 TLS 连接中断后，使用本机 uv 缓存离线安装成功，未改变验收用 `.venv4` 环境。

## 环境与证据

测试环境为 macOS 26.6.2 arm64、CPython 3.14.4、pypdfium2 5.13.0、PDFium 153.0.7999.0、Pydantic 2.13.5、NumPy 2.5.3、ONNX Runtime 1.30.0。同一 PDFium 动态库 SHA-256 为 `33c98063af28c0b7cbf8227f4422bf5c15942df2455cf7f0a5dce3dc601d52b0`。MinerU 源码固定为 `f504cff8b07776760c673023fab8847085158be0`。基线实际命中协议 24，最终版实际命中协议 29。

| 构建 | 源码及构建配置 SHA-256 | 扩展 SHA-256 |
| --- | --- | --- |
| v0.5.7 | `16d875a36632a37279125c26a52b6eac0392954f02ecd25fd2b334ee713f782c` | `9449a4ca67394f24f446b49de4de0f769a7ac15757db48b39f156a3401f11ad1` |
| 最终修复版 | `4d3e87afd3692566bb3f7ab12e5e1eb972c1aeb572a721a3dc09d70728d3048b` | `a7e86593dc078d50bbe3fe52cad5ca1154453a7e5daeeb41a507cbaac1a24029` |

`environment.json` 保存依赖版本、语料和冻结模型录制清单的哈希；`paired_compare.py` 与 `audit_reports.py` 保存运行和复核方法。`ablation-wrapper.patch`／`ablation-rust-owned.patch` 可在 `efc7bad` 上重建消融两变体，其间只有 textpage 调用链不同；原逐文档报告记录各变体实际扩展指纹。

恢复时保留前六份已经完整结束的配对，暂停中断的 `demo4.pdf` 整对重新测量。中断目录与日志另存，未计入正式结果。

修复工作区为 `/Users/myhloli/.codex/worktrees/rust-stage18-22-repair/docvortex`；独立基线为 `/Users/myhloli/.codex/worktrees/rust-stage057-baseline/docvortex`；消融包装变体为 `/Users/myhloli/.codex/worktrees/rust-stage20-wrapper/docvortex`。原仓工作区保持干净，原阶段报告未修改。本报告记录 0.5.8 发版前的修复验收；随后提交、推送及发布状态以 main 历史、`v0.5.8` 标签和 GitHub Release／PyPI 为准。验收期间的包版本仍为 0.5.7，发版仅更改版本元数据。

证据目录：`output/pdf/rust-stage18-22-repair/`（被 Git 忽略，保留在修复工作区）。全量测试、静态检查、输入审计、构建及安装日志均在该目录。复验配对可运行：

```sh
/Users/myhloli/projects/20240809magic_pdf/Magic-PDF/.venv4/bin/python output/pdf/rust-stage18-22-repair/paired_compare.py \
  --first /Users/myhloli/.codex/worktrees/rust-stage057-baseline/docvortex \
  --second /Users/myhloli/.codex/worktrees/rust-stage18-22-repair/docvortex \
  --suite public --rounds 3 --output output/pdf/rust-stage18-22-repair/recheck-public
```

Flash／shared 将 suite 和新输出目录对应改为 `flash`／`recheck-flash`、`shared`／`recheck-shared`，三条链路串行运行。已有正式报告由 `audit_reports.py --group final` 复核；冻结模型输入由 `audit_model_inputs.py` 复核。性能数字是上述冻结模型环境中的端到端对照，本轮未达到或宣称达到原定 2 倍目标。
