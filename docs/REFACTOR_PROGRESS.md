# DocVortex 渐进式重构与验证

基准提交：`b0cdd9a`。最终实现提交：`653f49d`。
工作分支：`codex/progressive-refactor`。六个阶段分为 14 个独立提交，验证记录另行提交。

公开 API、CLI、ModelJson/MiddleJson、Bundle、HTML 协议、七种输出格式和 MinerU
生产调用入口保持兼容。字体、语义判定阈值、文字映射和安装依赖组合保持原有策略。

## 实施结果

1. **基线与测量**：保留原生 PDF benchmark，增加公开流水线 benchmark，分别记录输入准备、分析、
   裁图、后处理、素材物化及七种输出耗时。每个文档单独启动进程，预热后计时 5 次；
   内存采样和 profiler 各自单独运行。保存完整协议、素材摘要、原始输出和离线 bundle。
2. **重复计算**：一张表只建立一次 glyph 来源索引；`AssetStore.copy()` 复制独立索引并共享不可变字节；
   显式格式输入跳过识别模块，Magika 在实际需要识别时加载。
3. **PDF 裁图**：先整理视觉块，再按物理页号选择连续区间，保留 64 页窗口及既有 DPI、旋转、
   JPEG 编码和素材命名。无图文档不启动裁图任务，异常路径也释放返回的页图。
4. **渲染与 Span**：前序文本、列表、表格候选由单次扫描维护；私有调用上下文只消费一次独占副本，
   实际合并的列表另行复制，保留 EPUB 标识及回调隔离。Span 拼接只重新规范化变化的边界，
   语言识别收到的内容和调用语义保持不变。代理码点清理改用等价正则。
5. **模块职责**：来源上下文下移到文档层，图片编码及 XML 名称工具下移到基础层，markup/MathML
   下移到内容层；HTML codec 通过资源协议依赖解析器。增加内部 raw block 和分析输入契约。
   DOCX 拆出字段/目录、样式、编号、表格及资源；PPTX 拆出形状、资源、样式、列表及标题。
   PDF 底层拆出坐标、对象、注解及字符证据，表格拆出检测、规则、注释及物化。
6. **几何热点**：两个热点阶段复用已经规范化的框进行裁剪，省去重复的坐标转换、排序和有限性校验。
   原有任意输入校验入口仍然存在，反向框的不同处理契约不混用，也没有跨文档几何缓存。

旧入口显式重导出同一实现。搬迁类型先在定义模块解析注解，再保留原有 pickle 模块身份，
因此独立导入新入口后也能调用 `get_type_hints()` 或构造 Pydantic `TypeAdapter`。
转换器每次处理重新创建可变状态，失败后复用实例与新实例的结果一致。

四个入口模块的行数分别从 3641/2329/2312/2445 变为 614/323/582/147；
原算法移入职责模块。PDF 底层和表格共 118 个函数的可执行 AST 在搬迁阶段保持一致。
文件识别到 EPUB/OFD 专用容器验证器的分派依赖保留，不复制其校验策略。

## 基于测量增加的裁图修正

仅筛选页面时，中文论文的进程总 RSS 出现了可重复的约 7% 回退。缩小批次能降低驻留内存，
但最初产生耗时回退。检查发现，进程池把“尚未达到最大容量”视为“正在创建进程”，
有空闲 worker 的小批量任务仍反复等待 100ms。

两个改动分别验证和提交：空闲 worker 复用不支付冷启动节流；连续页任务增加 **32 MiB 的四通道
像素估算预算**，同时保留最多 64 页的边界。单页超过预算时仍按原清晰度单独渲染。
该预算约束批量页图，不代表整个进程的 RSS 上限；既有进程池容量和超时配置保持原值。

96 页、96 张不同图片的真实合成 PDF 进一步验证了长文档：完整输出相同，解析中位耗时
从 1.643 秒降到 1.581 秒，采样进程总 RSS 峰值从约 4690 MiB 降到 1563 MiB。

## 验证证据

- **原生 PDF**：31 份语料的完整 Model/Middle 输出全部一致，耗时比值中位数约 0.976；
  各文档耗时与内存均未超过 5% 回退门槛。
- **公开流水线**：18 组真实/合成场景覆盖十五种原生输入、七种输出及长续接链，完整输出全部一致。
  ZIP 比较解包内容；EPUB 使用固定修改时间；PDF 比较逐页文字与像素，原始文件也保留。
- **差分检查**：2000 组行内序列与 2000 组混合块/渲染模式检查无差异，行内语言识别的输入序列也一致。
- **项目测试**：本地全量 460 项通过；补充注解回归后，最终 CI 的 macOS/Python 3.14 全量为
  461 项通过、1 项既有诊断排除。Python 3.10–3.14 的三平台矩阵、PDFium 5.13.0、
  Pydantic 最低版本、字体一致性和 lint 均通过。
- **真实平台产物**：最终实现与基准提交在每个平台的 62 个产物均逐字节一致，共 186 个文件。
  包括原始几何、Model/Middle、HTML/Markdown、素材和全部源页/布局 PNG。
  比较环境一致：Python 3.14.7、PDFium 5.10.1、Pydantic 2.12.5、NumPy 2.5.3、Pillow 12.3.0。
  另用 Poppler 渲染并目视检查了中文论文的语义 PDF，标题、正文、上下标和表格无新增问题。
- **独立安装包**：wheel/sdist 成功构建；全新 Python 3.13 环境不存在 MinerU、DocGale 或 pdftext，
  46 项检查通过，真实 CLI 转换的 Markdown 与基线相同。新增模块齐全，字体 SHA256 和 NOTICE 保持不变。
- **MinerU**：隔离工作树全量 3947 项通过，再补齐并通过 11 个本地 OFD 样本，合计 3958 项；
  保留原有 4 项跳过和 4 项排除。运行时代码未修改。两个测试文件的 7 个 mock 目标改为
  `table_materialization`，测试名称及断言不变，单独提交在 `codex/docvortex-refactor-tests`，
  提交 `e5a672b42`，同时提供可应用的 [测试适配补丁](validation/mineru-refactor-tests.patch)。
  原 MinerU 工作树及其分支保持原样。

完整指标见 [机器可读验证记录](validation/progressive-refactor.json)。
最终代码 CI：[653f49d 验证结果](https://github.com/myhloli/DocVortex/actions/runs/34059793389)。
本地产物位于 `.baseline/progressive-refactor/`；独立 MinerU 工作树位于其 `mineru-validation/` 子目录。

## 性能结果的解释

最终实现的公开流水线测量如下：

| 场景 | 解析耗时 | 进程总 RSS 峰值 |
| --- | --- | --- |
| 无图单页 PDF | 0.276 → 0.140 秒 | 450 → 184 MiB |
| 中文论文4（5页） | 0.805 → 0.661 秒 | 905 → 523 MiB |
| 财报（22页） | 3.849 → 3.595 秒 | 2708 → 859 MiB |

长续接链七种输出合计耗时从 0.622 秒降至 0.547 秒。

耗时采用同机、同依赖、预热后 5 次运行的中位数。RSS 每 50ms 采样父进程及裁图子进程，
求同一采样时刻的总和；共享页会被重复计入，不能解释为独占物理内存。
快阶段的微小比例波动单独复测，例如 ODT 输入准备/解析复测没有回退。

已验证框裁剪的 50000 次微基准从约 30.65ms 降至 9.34ms，
但财报整体原生分析在该单项改动前后仅变化约 0.6%。主要的可见收益来自避免无效裁图和减少页图驻留。

## 保留的基线问题

- 既有稀疏表格 gold 差异继续由单独诊断任务报告，没有更新或放宽断言。
- `caibao1.pdf` 的 PDF 导出在基准提交即存在 ReportLab `LayoutError`，因表格超出版面失败。
  该失败作为独立输出状态参与前后比较，不计为成功导出；其余六种输出照常比较。
  比较错误消息时仅去除不稳定的 Python 对象地址。

## 复现命令

```bash
python tests/benchmarks/flash_pdf.py --runs 5 --output .baseline/native-check
python tests/benchmarks/flash_pdf.py --pipeline --runs 5 --output .baseline/pipeline-check
python tests/benchmarks/pipeline.py --runs 5 \
  --baseline .baseline/progressive-refactor/pipeline-before \
  --output .baseline/pipeline-comparison
python -m pytest -q \
  --deselect tests/unittest/test_native_pdf_table_demo_manifest.py::test_demo_sparse_table_confidence_manifest
```

公开流水线的最终性能测量在 `653f49d` 执行；原生和压力测量在 `f05f69f` 执行，
随后只补充了搬迁类型的注解解析兼容修正，最终平台产物再次比较一致。

每次比较使用新的输出目录；`--runs 0` 只做功能捕获，不作为预热性能记录。
生成的 fixture 字节保存为 `source.bin`，比较时复用相同输入，避免 ZIP 创建时间污染来源摘要。
