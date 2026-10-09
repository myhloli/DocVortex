# PDF OCR model provenance

DocVortex independently implements ONNX inference and uses the following external model assets. Models are downloaded at runtime, not included in the Python wheel. No MinerU, PaddleOCR, or PaddleX Python source is included or imported.

| Component | Original model | Original revision |
| --- | --- | --- |
| Layout | [PaddlePaddle/PP-DocLayoutV2_onnx](https://huggingface.co/PaddlePaddle/PP-DocLayoutV2_onnx) | `7d44592493df02e28110d99de8ca4b1fbc7309bd` |
| Text detection | [PaddlePaddle/PP-OCRv6_tiny_det_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv6_tiny_det_onnx) | `2ba1506c0380b8f0b03dd142459aac66d4421f6c` |
| Text recognition | [PaddlePaddle/PP-OCRv6_small_rec_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx) | `b8f84f0b80c529de40b4fbb3544b84fa7233a513` |

These PaddlePaddle models are published under Apache-2.0; their original attribution and license apply to the downloaded weights and configuration data.

Distribution mirrors are [Hugging Face](https://huggingface.co/opendatalab/MinerU-4_models_onnx) revision `358310b4f64b95f9fefc372ad899356e4111f376` and [ModelScope](https://modelscope.cn/models/OpenDataLab/MinerU-4_models_onnx) revision `e916a9d96d645d005364abd7db8ffd4b7353cf4f`. The mirrors contain identical bytes for the six selected assets. DocVortex embeds sizes and SHA-256 hashes from the repository manifest and validates each downloaded file before installation. The OCR YAML files contain the runtime profile and recognition dictionary; no dictionary is read from a MinerU installation.
