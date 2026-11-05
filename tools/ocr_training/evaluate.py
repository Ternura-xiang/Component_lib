"""对已确认元件袋样本执行字段级 OCR 基线评估。

本工具有意复用正式应用的 ``RecognitionEngine``，从而保证评估结果对应实际发布链路，
而不是只测试脱离预处理和字段解析器的裸识别模型。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from component_library.recognition import RecognitionEngine


def load_manifest(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 清单并验证必要字段；格式错误时立即停止。"""

    samples: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item.get("image"), str) or not isinstance(item.get("expected"), dict):
            raise ValueError(f"第 {number} 行缺少 image 或 expected")
        samples.append(item)
    if not samples:
        raise ValueError("评估清单为空")
    return samples


def evaluate(manifest: Path) -> int:
    """运行推理并打印每字段完全匹配率；存在错误时返回非零状态。"""

    engine = RecognitionEngine()
    samples = load_manifest(manifest)
    totals: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    failures: list[str] = []
    for item in samples:
        image_path = (manifest.parent / item["image"]).resolve()
        draft = engine.analyze(image_path.read_bytes())
        for field_name, expected_value in item["expected"].items():
            totals[field_name] += 1
            actual = draft.fields.get(field_name)
            actual_value = actual.normalized_value if actual else None
            if str(actual_value) == str(expected_value):
                correct[field_name] += 1
            else:
                failures.append(
                    f"{image_path.name} {field_name}: expected={expected_value!r}, actual={actual_value!r}"
                )

    print(f"样本数: {len(samples)}")
    for field_name in sorted(totals):
        rate = correct[field_name] / totals[field_name]
        print(f"{field_name}: {correct[field_name]}/{totals[field_name]} ({rate:.2%})")
    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


def main() -> int:
    """解析命令行并执行评估。"""

    parser = argparse.ArgumentParser(description="元件袋 OCR 字段级评估")
    parser.add_argument("manifest", type=Path, help="UTF-8 JSONL 评估清单")
    arguments = parser.parse_args()
    return evaluate(arguments.manifest.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
