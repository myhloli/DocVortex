# DocVortex 移除 OpenCV 与 MinerU 迁移评估

2026-10-08。本轮基于 main `abfa99c63ce336da9da9916c5c9eb90937bbb3cf` 的 0.5.10、私有原生协议 31。实现纳入 0.5.11。DocVortex 已完成代码修改和本地验收；MinerU 仅做分析及集成验证，源码未修改。实验候选 wheel 沿用 0.5.10 版本用于同版本对照，未作为正式包发布。

## 0.5.12 候选：支持 MinerU 基础依赖迁移

第二轮基于已发布的 0.5.11 `9f3b9936f07cc5b2ad29e929e4e87dcbbdbf4f64`，在独立工作树
`/Users/myhloli/.codex-workspaces/worktrees/docvortex-opencv-free-20261008/docvortex` 中增加
`docvortex.image` 公共接口及 Rust/NumPy 参考内核。实现和验收阶段原 main 工作树保持原样，
本次整合将验收后的改动提交并合并到本地 main。新增通用函数已登记
`PUBLIC_API`；模型阈值和后处理选择留在 MinerU。接口契约见 [SDK 文档](sdk-0.4.md)。

图像基础数值规则固定保留整数与浮点灰度、线性/三次/区域/Lanczos 缩放、连续透视采样、
轮廓点序、组件顺序、旋转卡尺、扫描线填充、抗锯齿线及小型主元消元的历史舍入。
位图裁剪保持原协议；私有扩展因增加图像函数升级为 32。不会按 cv2 的可用性选择回退算法。
历史参考为本机 OpenCV 5.0.0.93；冻结样本包含宽图通道块的定点误差、非连续数组、uint16/
float32、边界裁剪、alpha、十六位 PNG、EXIF 和同一图像的 Python/Rust 一致性。

Python 全量测试 9642 passed、14 skipped；Rust workspace 16 项测试通过。完整 MinerU 集成
3010 passed、4 skipped。真实 ONNX/Torch、UNet 表格、Flash OCR、GGUF standard/advanced
在阻断 cv2 的进程内完成，协议、九种渲染和已采集模型输入摘要与冻结基线相同；Python 参考后端
也完成真实 ONNX 同样的输入和输出对照。另核对源 PDF 与基线/候选 PDF 导出，未出现新增视觉差异。
原有竖排表格导出的版式局限仍可见，本轮未修改它。

四条 MinerU 路径各五对独立进程的稳态中位数相对基线为 Flash -0.31%、ONNX +3.59%、
Torch +2.62%、表格 +3.24%，满足 <=5% 门槛。第一次表格轮次 +5.13% 超限后，改为连续
float32 字节传入几何内核并保持完整索引排序，241 项相关测试及 Clippy 通过后重新测量。
两轮证据保留，完整数值见 MinerU 迁移记录。

原始证据位于 `/tmp/mineru-opencv-free-20261008`，对应 MinerU 仓库的
`docs/next/opencv-loading.md` 记录完整路径与成对性能。当前 0.5.12 是本地候选，已纳入本地 main，尚未推送或发布；
以下章节保留 0.5.11 移除直接依赖时的历史验收记录。

## 实现与公开行为

`pyproject.toml` 删除 OpenCV 依赖，生产代码和引擎测试不再导入 `cv2`。

| 操作 | 当前实现 | 保留的行为 |
| --- | --- | --- |
| 位图裁剪、旋转及连续 BGR 像素 | 原有 Rust 内核 | 步长、边界、旋转方向、所有权及协议 31 均不变 |
| Python 旋转参考路径 | `numpy.rot90(...).copy(order="C")` | 90/180/270 度返回独立连续数组；其余角度返回原对象 |
| RGB/BGR 对比度 | NumPy | uint8/uint16 保留 15 位定点灰度系数和舍入；float32 保留灰度权重；统计公式及两位小数不变 |
| Python 与 Rust 裁图 JPEG | 共用 Pillow 编码函数 | `quality=95`、`subsampling=2`、`optimize=False`、`progressive=False`；保持 data URI 格式 |

Pillow 的 `subsampling=2` 表示 4:2:0，参数含义见 [Pillow JPEG 文档](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg)。原生 BGR 只在编码边界交换为 RGB，没有改动 Rust 算法或升级协议。新增编码函数使用 `contextlib.closing` 和 `BytesIO` 上下文管理，成功与失败都释放临时图像及缓冲。

有效的三/四通道 uint8、uint16、float32 图像保留既有语义，包括忽略 alpha 和高位深 JPEG 的饱和舍入。对比度统计的非法形状、空图及不支持的位深改为 `ValueError`；进入 JPEG 编码的非法形状或位深同样使用 `ValueError`，不再暴露已移除库的 `cv2.error`。空裁框、空裁片以及编码器 `OSError` 返回空字符串。公开模块清单、现有函数签名、ModelJson/MiddleJson 和素材命名保持原样。

## 本地验收

环境：Apple M4，macOS 26.6.2 / arm64，Python 3.13.5，NumPy 2.5.3，Pillow 12.3.0，PDFium Python 包 5.10.1。两侧分别从冻结源码和候选源码构建 wheel，再隔离安装，未复用宿主 DocVortex 安装。候选环境确认没有 `cv2` 模块或 OpenCV 依赖；基线为 OpenCV 5.0.0.93。全部依赖约束保存在外部证据目录的 `constraints.txt`。

| 检查 | 结果 |
| --- | --- |
| Rust 后端完整 Python 测试 | 9490 passed、1 skipped，74.69 秒 |
| Python 后端完整 Python 测试 | 8388 passed、1103 skipped，141.77 秒 |
| 图像统计、旋转、JPEG、空框、失败与资源释放 | 34 项通过；最终 JPEG 测试同时核对采样层信息 |
| 相关原生裁剪及边界检查 | 102 项通过；最后复查架构守卫、图像和位图裁剪 82 项通过 |
| Rust fmt / clippy / core 单元测试 | 全部通过 |
| Python Ruff 检查及格式 | 全部通过 |
| uint8 灰度定点公式与旧灰度转换对照 | 穷举 16777216 种 RGB 颜色，逐像素零差异 |
| RGB/BGR 对比度额外对照 | uint8、uint16、float32 共 1800 个四通道测试组合，公开返回值零差异 |
| 历史 Flash 语料 | 19 份、168 页；完整 model/geometry JSON 字节相同，页面语义及框指纹相同 |
| OpenDataLoader 语料 | 所有轮次的 200 份 Markdown 和 54 个素材逐字节相同；每轮真实新增 200 次原生文本快照 |
| Office 实际转换 | DOCX/DOC、PPTX/PPT、XLSX/XLS 共六份、24 页，协议、Markdown 和全部素材共 86 个文件相同 |

完整测试采用现有 CI 的 deselect，仅排除既有稀疏表格 confidence 诊断用例。Python 后端的跳过主要为原生扩展专属检查；通过计数没有合并计算。

历史基线的 168 页输出均完整写出，两个基线进程随后在解释器退出时出现 `recursive_mutex lock failed: Invalid argument`，退出码 134。候选历史回放退出码为 0。两份基线页面数据相同，且候选完整 model/geometry 与基线字节相同；本轮没有定位该原生析构问题的根因，因此不把旧基线进程记为正常通过。正式性能实验的全部基线和候选进程均正常退出。

JPEG 的严格字节金样限定在冻结的 OS、CPU 架构、Pillow 和 libjpeg-turbo 版本下。其他环境检查尺寸、方向、模式、量化表、采样层、非渐进编码及解码像素差异。人工核对了源 PDF 的两块彩色图与导出裁图，并查看真实语料中的柱状图、表格；本轮全部素材与基线逐字节相同。

CI 的 Ubuntu / Windows / macOS、Python 3.10 / 3.14、Python / Rust 矩阵增加了独立安装无 `cv2` 检查。本报告记录本地验收；0.5.11 的远端跨平台 CI 和发布结果以对应 GitHub Actions、GitHub Release 与 PyPI 为准。

## 性能与评分

计时覆盖延迟导入和完整 `convert`：解析、后处理、Markdown 和素材写盘。解释器启动、依赖安装、输入/输出摘要及评分在计时外。

三对冷启动使用独立安装路径的环境克隆，并移除两侧全部字节码。每个新进程先转换一份与语料无关、含两张 JPEG 导出的四页 PDF，再完整转换 200 份单页 PDF。五对稳态使用各自基础安装环境，每轮仍是独立进程，先转同一无关 PDF，再真正提取全部语料；轮换两侧运行顺序。没有跨文档解析结果缓存。

| 条件与中位数 | 原版 0.5.10 | 无 OpenCV 候选 | 变化 |
| --- | ---: | ---: | ---: |
| 三对独立安装位置：首份含图 PDF | 18.3535 秒 | 3.9464 秒 | -78.50% |
| 五对稳态：完整 200 页 | 9.1992 秒 | 9.4109 秒 | +2.30% |

稳态候选/基线比为 1.0230，满足约定的 <=1.05 门槛。该收益主要针对新安装位置的首次依赖加载；各轮原始时间及首份耗时保留在 `performance.json`。未清空操作系统页缓存，冷启动结果不代表冷盘性能。RSS 仅为同一计时进程的辅助记录，本轮没有独立内存性能结论。

使用原有、未修改的 OpenDataLoader evaluator 分别重算两侧输出，200 份逐文档分数及汇总均相同：

| 指标 | 两侧相同分数 |
| --- | ---: |
| Overall | 0.9076450990 |
| NID | 0.9147262065 |
| TEDs | 0.9207865250 |
| MHS | 0.8876147653 |

NID 200 份、TEDs 42 份、MHS 107 份；缺失预测为 0。本轮固定 PDFium 5.10.1，既有发布版 benchmark 使用不同 PDFium 版本，README 的性能/评分表保持原样。

## MinerU 接入验证

验证当前 MinerU checkout `f504cff8b07776760c673023fab8847085158be0`，在其 Python 3.14 本地环境中分别覆盖实际基线/候选 wheel 的模块和元数据，记录真实导入路径。共享环境中的旧 DocVortex 安装未修改。

- 54 项现有导入边界、协议、模式路由、页面渲染调度及 Office 格式测试通过。
- 三份 PDF、11 页，真实 `analyze_pdf(effort="flash", parse_mode="auto")` 均自动分类为 txt；完整 model-list 和 layout geometry 字节相同。仅封锁模型工厂与 VLM 初始化，没有替换分类或原生解析。
- 同三份 PDF 的同步 Flash txt 包装输出，MinerU 九种格式和 DocVortex 七种格式、完整协议及素材共 78 项比较通过。容器比较仅沿用现有工具允许的时间字段及经复算确认的 producer 标识归一化。
- medium/high 按冻结 Flash 布局回放真实 PDF 表格优先和文本回填，共六个组合输出相同。demo2 的两张表两档均接受；每页字符几何提取最多一次。本组没有触发 OCR 回退，未运行实际 OCR、公式或 VLM 模型推理。

以上证明升级 DocVortex 后当前接入保持兼容；MinerU 自己仍安装并使用 OpenCV。

## MinerU 是否也能移除

当前不能直接删除其 OpenCV 依赖。可以先拆出 Flash 文本路径，再迁移基础像素操作；要完全移除，还必须重做模型敏感的图像内核，并处理推理后端的依赖。

静态 AST 审计找到 **26 个生产文件、152 处直接 `cv2.*()` 调用**；共有 28 个生产文件引用 `cv2`，另两个仅保留导入。直接调用中，`cvtColor` 31 处、`resize` 25 处、`imdecode` 4 处、`rotate` 3 处。数量表示静态调用点，包含可选模型、旧辅助路径和调试绘图，不等于一次解析会执行 152 次。

| 路径 | 当前用途与实际入口 | 迁移判断与必要验收 |
| --- | --- | --- |
| 公共 Parser、API 客户端、VLM 客户端 | `mineru.parser`、`mineru.parser.api_client`、`mineru.model.vlm.client` | 实际阻断 cv2 导入后均可正常导入；保持这条轻依赖边界 |
| Flash txt | `pipeline.py → window.py → PdfModel().predict`，按需调用 DocVortex 补图后立即返回 | 运行分支具备独立条件。先隔离模型/混合分析的提前导入，验证 txt、auto->txt、异步包装与含图页 |
| Flash OCR / medium / high / xhigh | OCR、公式、表格预处理和原生文本回填中的 RGB/BGR 转换 | 仍会进入本地模型路径，不能仅靠惰性导入去除运行时依赖 |
| 通道、灰度、split/merge、直角旋转、遮罩、边框 | `model/ocr/image.py`、后端 OCR/公式/表格、方向分类器 | NumPy 或通用 Rust 像素内核可以替换；冻结 BGR/RGB、alpha、位深、舍入、步长及返回数组所有权 |
| 图片解码 | OCR `check_img`、内部 DecodeImage 等 | Pillow 可作为候选；需检查灰度/调色板、16 位、透明通道、EXIF 方向和错误返回约定 |
| 25 处缩放 | Layout 800x800、OCR 识别宽度归一化、表格分类/SLANet、公式预处理 | 当前存在 LINEAR、CUBIC 等配置。Pillow/其他后端不能默认等价；逐个冻结输入张量并验证模型输出与置信度 |
| OCR DB 后处理 | `db_postprocess.py`，Torch/ONNX OCR 都使用 | findContours、minAreaRect、approxPolyDP、fillPoly、mean、dilate 参与框恢复和打分。需要轮廓、洞、顺序、点坐标及阈值裁决一致 |
| 表格 UNet / 线恢复 | `table_structure_unet.py`、`utils_table_line_rec.py` | 形态学、连通域、最小矩形、仿射变换影响分支和最终 HTML；应以线段、网格、旋转及真实表格结果验收 |
| OCR/公式透视裁剪、印章展平 | `get_rotate_crop_image`、`seal_crop.CropByPolys`、`AutoRectifier` | 投影矩阵、CUBIC 重采样和 BORDER_REPLICATE 需专门实现；包含异常点及小噪声框回退 |
| 公式裁边 | PP-FormulaNet、UniMERNet 的 findNonZero/boundingRect 等 | 需保留阈值、裁边、padding 和模型输入数值，随后验证 LaTeX 输出 |
| 调试绘图 | imwrite、polylines、putText 等 | 可以隔离或单独迁移，不应与生产 OCR/表格算法混为同一优先级 |

实际阻断 cv2 的 PDF 管线导入失败链为：`pipeline.py:19 → model/runtime/hybrid.py:19 → table/cls/mineru_table_ori_cls.py:7`。除此之外，`window.py` 还提前导入含 cv2 的 formulas、ocr、tables 和 text 模块。只延迟 `HybridLocalModelContextSingleton` 不足以解除整个导入闭包。Flash txt 的早返回分支已由源码和真实自动分类回放确认；先拆分该分支具有可行性，但当前 checkout 仍无法在无 cv2 环境中导入 PDF 管线。

`seal_det_warp.py` 中存在 calibrateCamera、Rodrigues 和 remap。当前 `seal_crop.py:401` 显式使用 `mode="homography"`；不能把库中的 calibration 模式一概视作当前印章主路径必经操作，仍需保留其他可调用模式或明确调整支持范围。[OpenCV 几何变换文档](https://docs.opencv.org/4.13.0/da/d54/group__imgproc__transform.html)描述了插值和边界参数，本轮没有假定 Pillow 变换能逐像素替代这些配置。

### 推理依赖仍可能安装 OpenCV

本地安装元数据及固定版本的 PyPI 元数据复核结果如下。直接依赖检查与全平台依赖解析是不同证据，后者本轮没有执行。

| 包或额外依赖 | 核对结果 |
| --- | --- |
| DocVortex 当前候选 wheel | 必需和可选依赖均无 OpenCV，隔离安装实际无 cv2 |
| mineru-vl-utils 2.0.5 | 基础依赖无 OpenCV；可选 vllm/lmdeploy 会进入其他推理栈。本机原安装为 2.0.4，2.0.5 以固定 PyPI 元数据复核 |
| mineru-llama-cpp 0.1.2 | 基础依赖无 OpenCV；本地包源码未发现 cv2 引用 |
| ModelScope 1.40.0 | MinerU 当前使用的基础依赖无 OpenCV；`cv`、`multi-modal` 等额外依赖明确包含 OpenCV |
| Linux full 的 vllm 0.19.1 | 明确要求 `opencv-python-headless>=4.13.0`；见[固定版本上游依赖](https://github.com/vllm-project/vllm/blob/v0.19.1/requirements/common.txt) |
| Windows full 的 lmdeploy 0.17.0 | 固定 [PyPI 元数据](https://pypi.org/pypi/lmdeploy/0.17.0/json)明确要求 `opencv-python-headless` |
| Windows 的 qwen-vl-utils 0.0.14 | 固定 [PyPI 元数据](https://pypi.org/pypi/qwen-vl-utils/0.0.14/json)列出 av、packaging、Pillow、requests；无直接 OpenCV 依赖 |

`opencv-python-headless` 仍提供 cv2，只去掉 GUI 部分，因此改为 headless 不满足完全移除目标。对于当前 full 配置，即使全部 MinerU 自有调用迁走，上述固定版本的推理后端也会继续安装 OpenCV。后续需要按支持的平台、后端及 extras 分别检查解析后的依赖闭包；没有验证的新版本不能沿用本表的结论。

建议迁移顺序：先建立无 OpenCV 的 Flash txt 导入与实际运行检查；再迁移有精确像素金样的基础操作；之后按模型分别处理 resize、轮廓/形态学、透视及印章路径，并用真实 OCR、公式和表格结果裁决。完整去除 full 安装中的 cv2 还需要选择或调整对应推理依赖。本轮不修改 MinerU 的源码、依赖声明或支持范围。

## 原始证据与复查

本机证据根目录：`/Volumes/2TB/projects/20240809magic_pdf/opendataloader-bench/output/docvortex-opencv-removal-20261008`。

- `baseline-identity.json`、`baseline-head.tar`、`baseline-wheels/`、`candidate-wheels/`：冻结身份与实际安装轮子。
- `compare_wheels.py`、`performance.json`、`performance.log`、`runs/*/{report,hashes}.json`：同机协议、原始时间及全部文件摘要。
- `prediction/{baseline,candidate}/evaluation.json`、`evaluation-comparison.json`：未修改 evaluator 的逐文档和汇总分数。
- `pytest-{python,rust}.log`、`cargo-test.log`、`grayscale-equivalence.json`、`history-comparison.json`：本地检查及完整历史回放差分。
- `compare_office.py`、`office-comparison.json`、`office-{baseline,candidate}/`：六种 Office 输入的完整输出。
- `mineru-tests.log`、`host-boundary-comparison.json`、`host-auto-comparison.json`、`host-native-{baseline,candidate}/`：真实宿主及固定布局原生阶段验证。
- `probe_without_cv2.py`、`import-probes/`、`mineru-callsite-audit.json`、`mineru-optional-package-metadata.json`：可重放的导入阻断、调用点及上游依赖证据。

0.5.9 的早期文件已加 `0.5.9-superseded` 后缀保留，不参与本报告结论。依赖安装及构建日志位于证据目录或同名 `/tmp/docvortex-opencv-*` 日志；原实验未执行提交、推送、版本变更或发布动作，后续发布使用 0.5.11。
