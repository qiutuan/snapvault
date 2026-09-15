# ADR-0002: OCR 选型 — RapidOCR（PaddleOCR ONNX 模型离线打包）

- 状态：已采纳

## 背景
默认简体中文 + 英文；模型必须离线打包；机器可能是无 GPU 的低配（测试/售后同事）。

## 备选
- PaddleOCR 完整框架：识别精度最高，但 paddlepaddle 依赖庞大（数百 MB）、安装易碎。
- Tesseract：中文识别率一般。
- RapidOCR（rapidocr_onnxruntime）：复用 PaddleOCR 蒸馏后的 ONNX 模型（det/rec/cls），
  纯 onnxruntime 推理，无 paddle 依赖。

## 决策
采用 **RapidOCR**，det/rec/cls 三个 ONNX 模型（约 16MB）放入
`core/snapvault/models/rapidocr/` 随包分发（pyproject package-data），
运行时从包内加载，零下载、零网络。

## 后果
- 中文简体 + 英文识别质量与 PaddleOCR 蒸馏模型一致，单张延迟 <1s（CPU）。
- 模型缺失时抛 `OCRUnavailableError` 并给出明确修复指引。
