# 识别模块：图像、条码与字段解析

代码：`recognition.py`、`label_parser.py`、`model_manifest.json`；测试：`test_label_parser.py`；样本记录见 [实物袋贴](../REAL_LABEL_SAMPLES.md)。

## 识别管线

```text
照片/视频帧 → 缩放与画质指标 → 标签定位/透视矫正
                              ├→ color/CLAHE/binary × 多方向 OCR
                              └→ ZXing 二维码/条码
                                     ↓
                            字段规则 + 冲突检查
                                     ↓
                              LabelDraft（草稿）
```

`RecognitionEngine.analyze(image_bytes)` **不应写数据库**。识别失败或 OCR 与二维码互相矛盾时保留警告，等待用户核对。

## 模型与离线行为

当前使用 RapidOCR、ONNX Runtime 和随包模型。`status()` 根据 `model_manifest.json` 的哈希核对预期模型状态；应用不应为了识别而在运行中自动下载模型。修改模型时应同步更新版本、来源、许可证和 SHA-256，并重新测试识别。

## 调试步骤

1. 检查画质：失焦指标、过曝比例和图像方向。
2. 确认标签定位/透视矫正是否正确；定位失败时应保留整图回退路径。
3. 比较颜色、对比度增强、二值化分支及旋转方向。
4. 检查 OCR 文字框、阅读顺序与识别置信度。
5. 检查 `label_parser.py` 对编号、型号、数量、封装的规则及冲突处理。
6. 检查二维码结构化字段与 OCR 的交叉验证是否合理。

## 新增标签规则

先在 `tests/test_label_parser.py` 添加真实或脱敏文本的回归样例。必须防止将日期、物流号、电压、电流当作型号或 MPN。不能靠“猜测填满字段”掩盖低置信度。

OCR 评估脚本和样本清单见 [`tools/ocr_training/README.md`](../../tools/ocr_training/README.md)。现有规划建议先完成至少约 100 张独立袋贴的基线；达到更大规模的校正样本后再考虑微调，而不是在缺少评估时立即训练。
