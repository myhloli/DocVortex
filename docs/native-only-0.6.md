# 0.6 原生后端迁移与本地验收

0.6 保留 Python SDK、CLI、文档协议和多格式解析与导出，计算后端统一为必需的 Rust native 扩展。公开函数签名和有效输入的输出语义保持不变。

## 安装与迁移

- 支持启用 GIL 的 CPython 3.11–3.14；不支持 PyPy 和 free-threaded Python。
- 依赖为 `pypdfium2>=5.13.0,<6`，测试统一固定 `5.13.0`，删除 5.10 测试矩阵。
- 正式发行物仅为五个平台的 `cp311-abi3` wheel：Linux x86_64/aarch64（manylinux_2_28）、Windows x86_64/ARM64、macOS arm64（macOS 14+）。不再发布纯 Python wheel 或 sdist。
- 从 shell、CI 和系统配置中移除 `DOCVORTEX_COMPUTE_BACKEND`、`DOCVORTEX_BUILD_NATIVE`。包括 `rust`、`1`、`auto` 和空字符串在内，任何已设置值都报错；变量在 native 加载缓存建立后出现，也会被拒绝。
- 源码开发必须安装 Rust 工具链和平台链接器，再执行 `uv pip install -e ".[test,dev]"`。构建 wheel 使用 `uv build --wheel`。缺少 Cargo 或编译失败不会产生兼容包。
- 需要旧平台或纯 Python 包的用户可固定使用 `docvortex<0.6`。安装本版本不再提供自动降级。

扩展仍惰性加载，私有协议升级为 36。缺失、损坏或协议不匹配的扩展会报告安装问题并保留原始异常；更新 editable 安装后应重新构建并重启使用它的进程。

## 实现边界

删除字符读取、去重、文本分组、排版、上下标、分栏、表格几何与图像数值核的重复 Python 参考实现。少量行、空结果、非有限数值、巨大整数、重复来源和排序平局也进入原生实现；特殊数值在 Rust 绑定中借助当前解释器算术保持精度和版本语义。

Python 继续负责公开对象、Unicode 与参数准备、文档语义、格式分派、渲染和导出。原本只有一份实现的 NumPy/Pillow 工具仍保留；Form 的 pypdf 复杂语法解码仍属于输入适配。PDFium 的同库 ABI 校验、锁、函数强引用和句柄生命周期保持不变。

公式连通扫描逐次消费当前未认领候选，保持最小种子、升序邻接、栈顺序和后续标记认领，不构造平方大小的邻接表。表格小输入与特殊数值通过原生通用入口处理，保留原坐标对象及 Python 3.11/3.12+ 的求和语义。

测试中的旧差分入口改为固定数据或独立契约断言。固定数据来自 `2c1ec66ee52bb37135599af92aa175bd77fc04d7`、协议 35 和 pypdfium2 5.13.0，不在测试时加载历史后端。

## 本地验收记录

本轮在 Apple M4、16 GiB 内存上验证，主环境为 CPython 3.14.4 / pypdfium2 5.13.0。

| 验收项 | 结果 |
| --- | --- |
| 完整 Python 测试 | 10,675 passed；1 skipped（本机没有 TeX Live） |
| Rust core 测试 | 8 passed |
| Rust 与 Python 静态检查 | Cargo fmt、Clippy（`-D warnings`）、Ruff、actionlint 通过 |
| 历史 PDF | 19 份 / 168 页，完整模型 JSON 与几何指纹零差异 |
| 公开解析比较 | 16 份 / 128 页，ModelJson、MiddleJson、素材哈希和诊断全部等价 |
| MinerU 边界集成 | 519 passed，保留原有 dirty 测试与 examples |
| MinerU 真实 PDF 回放 | 3 份 PDF × medium/high，共 6 组；表格、文本、回退输入与调用计数全部等价；未执行模型推理 |
| 已安装 wheel | CPython 3.11/3.14 独立环境通过；demo1、demo2、120×7 密集表格的全部单元格内容与顺序通过 |
| ABI3 | 同一 macOS wheel 在 CPython 3.11、3.12、3.13、3.14 导入并执行原生内核通过 |
| 依赖与包完整性 | 两个独立环境依赖检查、twine check 通过；432 个包文件与当前源码逐字节一致 |
| 失败路径 | 缺少 Cargo 的真实源码构建失败且无产物；公开 parse 拒绝旧环境变量；缺失/损坏扩展与旧协议测试通过 |

原有稀疏表格 diagnostic 用例在旧 native 基线和新候选的 PDFium 5.13 环境均通过，新候选在 Python 3.11/3.14 也通过。因此取消该用例的 CI 排除及非阻断作业，恢复为正常测试门禁。

性能比较按文档交替执行旧、新 native 版本，每份文档预热后计时 5 次，峰值 RSS 在独立进程测量。公开 `parse()` 的逐文档耗时中位数之和为 **12.8197 → 12.8356 秒（+0.12%）**；逐文档峰值 RSS 比值的中位数为 **0.9971**。没有文档触发工具的 5% 复测门槛。本轮不提出额外提速结论。`small_ocr.pdf` 的公开解析会按既有契约返回 `pdf_ocr_unsupported`，不计入公开解析性能样本；其内部原生布局仍包含在历史语料回放中。

候选文件为 `docvortex-0.6.0-cp311-abi3-macosx_14_0_arm64.whl`：

- wheel SHA256：`b58bb040b76d75e3e6c1a2cc2d9671f39ac6099d83f499ef76038ebf6b332090`
- 扩展 SHA256：`ca12796dcf4396ae0bc9e9f50f68225df19ad5140d7914157f97940d443de443`
- 本地保留位置：`build/native-only-0.6/wheelhouse/`；基线、验证日志与摘要保留在 `build/native-only-0.6/`。完整配对性能报告位于 `/tmp/docvortex-native-only-0.6-01a163dc/performance/report.json`。

## 正式发布门禁

本地验收不替代远程平台验收。正式发布仍要求最终代码的 main CI 绿灯、五个平台最终 wheel 全部通过安装/ABI/内容验证，并核对完整产物集合；使用 `skip_ci=false` 发布，随后从 PyPI 安装精确版本复验。

发布前的集合守卫不可跳过：必须恰好包含五个预期平台 wheel，每包只有一个原生扩展，文件名、WHEEL、METADATA 与 release tag 一致，拒绝混入通用 wheel 或源码包。

本轮未执行推送、tag、GitHub Release 或 PyPI 发布。实际发布时间由维护者决定；验收不满足时延后发布，问题发行物应撤回并发布修复版，不恢复 Python 计算后端。
