# OCR 评估与后续微调

正式应用只包含 ONNX 推理模型。本目录用于样本量达到门槛后的离线评估和微调，
不会被 PyInstaller 打入安装包。

评估清单采用 JSON Lines，每行描述一张已人工确认的矫正标签图：

```json
{"image":"samples/label-001.png","entity_id":"bag-001","expected":{"lcsc_part_number":"C270654","quantity":"100"}}
```

运行基线评估：

```bash
.venv/bin/python tools/ocr_training/evaluate.py evaluation.jsonl
```

`entity_id` 表示实体袋贴。制作训练集和测试集时必须按该字段分组，禁止把同一袋贴的
不同拍摄角度放入两个集合。达到规划中的样本量和失败模式门槛后，可使用 PaddleOCR
训练工具微调识别模型，再导出 ONNX。替换模型时必须更新 `model_manifest.json` 的版本、
来源、许可证和 SHA-256，并重新运行本脚本与完整测试。

