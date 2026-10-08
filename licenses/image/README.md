# 图像数值规则来源

DocVortex 的图像接口独立实现，不链接或导入 OpenCV。用于保留历史像素行为的缩放、轮廓、旋转卡尺、扫描线、抗锯齿和小型矩阵规则参考 OpenCV 5.x、Carotene 和 KleidiCV 26.03。

- [OpenCV 5.x](https://github.com/opencv/opencv/tree/5.x)：Apache-2.0，许可见 LICENSE-OPENCV。
- [Carotene](https://github.com/opencv/opencv/tree/5.x/hal/carotene)：Copyright (C) 2015 NVIDIA Corporation，BSD-3-Clause，许可见 LICENSE-CAROTENE。
- [KleidiCV 26.03](https://gitlab.arm.com/kleidi/kleidicv/-/tree/26.03)：Copyright 2024-2026 Arm Limited，Apache-2.0。

相关文件为 foundation/_image_numeric.py、rust/docvortex-core/src/image_numeric.rs 和 image_drawing.rs。其余项目代码维持原许可。
