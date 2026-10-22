# Windows ARM64 原生扩展

DocVortex 的 Windows ARM64 native wheel 面向普通 GIL 版 CPython 3.11–3.14；从 0.6.0 起所有支持平台的 Python 下限统一为 3.11，公共 SDK 和 CLI 保留，Rust 扩展为必需依赖，不再发布纯 Python 兼容 wheel 或 sdist。

当前源码忽略旧 `DOCVORTEX_BUILD_NATIVE` 和 `DOCVORTEX_COMPUTE_BACKEND` 环境变量，任意值均不影响构建或运行。此兼容性修复尚未发布；已发布的 0.6.0 包仍会拒绝这两个变量。

## 构建与安装

静态 Native wheels 矩阵使用 `windows-11-arm`、`ARM64`，以 Python 3.11 构建 `cp311-abi3-win_arm64` wheel。产物通过原有收集和 Publish 流程发布，无额外发布开关。

ONNX Runtime 最低版本为 1.24.4；Windows ARM64 使用普通 `onnxruntime`，Windows x64 使用同一下限的 `onnxruntime-directml`。

Windows ARM64 依赖普通 `reportlab`，使用其 Python 实现；其他平台继续依赖 `reportlab[accel]`。

`metafile-render>=0.3.0,<1.0.0` 及其 pyclipper 实现保持不变。普通 `pip install docvortex` 优先使用依赖的 wheel；pyclipper 没有适配 wheel 时，pip 可以从源码构建。此时用户需要 Visual Studio Build Tools 的 ARM64 C++ 工具和 Windows SDK。CI 配置 ARM64 MSVC 环境，但不维护 pyclipper 补丁或分发其 wheel。

只保证 DocVortex 自身扩展提供预编译 wheel，不保证整个依赖集合都免编译安装；不要用全局 `--only-binary=:all:` 阻止 pyclipper 的源码安装。

## 验收

主 CI 独立检查同一 ARM64 wheel 在 Python 3.11、3.14 上的 ABI。工具核验真实 ARM64 解释器、64 位指针、wheel 标签、PE 指令集 `0xAA64` 和实际安装来源。

Native wheels 工作流还以同一个发行物建立两个干净环境，覆盖 Python 3.11/3.14 和 PDFium 5.13.0；执行 `pip check`、原生加载、真实 PDF 的固定语义预期、密集表格全部 840 个单元格、PDFium 桥接及无 rl_accel 的 PDF 导出检查。安装校验允许 pyclipper，要求环境不包含 rl-accel，且 reportlab 使用 Python 实现。

## 验证边界

0.5.12 将 Windows ARM64 构建与安装校验纳入既有发布流程；metafile-render 保持既有依赖范围。

本机为 macOS ARM64，本机回归不能替代 Windows ARM64 runner 的实际构建和安装结果；跨平台终态须以 CI 日志为准。
