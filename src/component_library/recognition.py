"""完全离线的袋贴图像识别管线。

重型依赖采用延迟导入，使数据库、API 契约和测试在没有摄像头/OCR 运行库的环境中
仍可启动。正式安装包会固定并携带这些依赖和模型，不会在运行时下载模型。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .label_parser import merge_fields, parse_ocr_lines, parse_qr_payload
from .models import LabelDraft, OcrLine


@dataclass(slots=True)
class PreparedImage:
    """一个可送入 OCR 的图像分支。"""

    name: str
    image: Any


class RecognitionEngine:
    """延迟加载 OCR 的单实例识别器。

    ONNX Runtime 会维护较大的模型会话，因此整个进程共享一个实例。锁用于防止摄像头
    连续帧同时进入非线程安全的 OCR 管线。
    """

    def __init__(self) -> None:
        self._ocr: Any | None = None
        self._lock = threading.Lock()
        self.model_version = "RapidOCR/ONNX (not loaded)"

    def status(self) -> dict[str, Any]:
        """返回依赖和模型校验状态，不触发联网下载。"""

        try:
            import cv2  # noqa: F401
            import onnxruntime  # noqa: F401
            import rapidocr
            import zxingcpp  # noqa: F401
        except ImportError as error:
            return {"available": False, "model_version": self.model_version, "error": str(error)}
        try:
            manifest_path = resources.files("component_library").joinpath("model_manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            model_directory = Path(rapidocr.__file__).parent / "models"
            mismatches = []
            for filename, expected in manifest["models"].items():
                path = model_directory / filename
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    mismatches.append(filename)
            if mismatches:
                return {
                    "available": False,
                    "model_version": manifest["runtime"],
                    "error": f"模型缺失或校验失败：{', '.join(mismatches)}",
                }
            self.model_version = manifest["runtime"]
        except (OSError, KeyError, json.JSONDecodeError) as error:
            return {"available": False, "model_version": self.model_version, "error": str(error)}
        return {"available": True, "model_version": self.model_version, "backend": "onnxruntime"}

    def analyze(self, image_bytes: bytes) -> LabelDraft:
        """识别一张图片并返回待确认草稿，不产生任何数据库副作用。"""

        import cv2
        import numpy as np

        encoded = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            return LabelDraft(warnings=["图片无法解码"])
        image = self._resize(image, 1600)
        quality = self._quality(image)
        prepared = self._prepare_variants(image)
        barcode_payload = self._decode_barcodes(prepared)

        with self._lock:
            lines, branch, recognized_image = self._recognize_best(prepared)
        ocr_fields = parse_ocr_lines(lines)
        barcode_fields = parse_qr_payload(barcode_payload or "")
        fields, warnings = merge_fields(ocr_fields, barcode_fields)
        if float(quality["blur_score"]) < 45:
            warnings.append("画面可能失焦，请重新拍摄并确认所有字段")
        if float(quality["overexposed_ratio"]) > 0.22:
            warnings.append("标签存在明显过曝或反光")
        if not lines:
            warnings.append("未识别到印刷文字；可以重新拍摄或手工录入")
        if barcode_payload and not barcode_fields:
            warnings.append("检测到条码，但内容不是已知袋贴格式")
        annotated = self._annotated_png(recognized_image, lines)
        return LabelDraft(
            fields=fields,
            ocr_lines=lines,
            raw_barcode=barcode_payload,
            model_version=self.model_version,
            preprocessing=branch,
            quality=quality,
            warnings=warnings,
            annotated_image_png=annotated,
        )

    @staticmethod
    def _resize(image: Any, maximum: int) -> Any:
        """等比例缩小大图，避免 OCR 延迟和内存随相机分辨率失控。"""

        import cv2

        height, width = image.shape[:2]
        scale = min(1.0, maximum / max(height, width))
        if scale == 1.0:
            return image
        return cv2.resize(
            image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
        )

    @staticmethod
    def _quality(image: Any) -> dict[str, float | bool | str]:
        """计算失焦和过曝指标；指标只用于拒绝过度自信，不直接修正文字。"""

        import cv2
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return {
            "blur_score": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            "overexposed_ratio": float(np.mean(gray > 245)),
            "width": float(image.shape[1]),
            "height": float(image.shape[0]),
        }

    def _prepare_variants(self, image: Any) -> list[PreparedImage]:
        """定位白色标签并创建原色、增强灰度和二值化三个 OCR 分支。"""

        import cv2

        label = self._rectify_label(image)
        gray = cv2.cvtColor(label, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        binary = cv2.adaptiveThreshold(
            clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )
        return [
            PreparedImage("rectified-color", label),
            PreparedImage("rectified-clahe", cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR)),
            PreparedImage("rectified-binary", cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)),
        ]

    @staticmethod
    def _rectify_label(image: Any) -> Any:
        """尝试把最大的明亮四边形矫正为矩形，失败时保留整图。"""

        import cv2
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        mask = cv2.inRange(gray, 135, 255)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        minimum_area = image.shape[0] * image.shape[1] * 0.08
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            if cv2.contourArea(contour) < minimum_area:
                break
            perimeter = cv2.arcLength(contour, True)
            polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if len(polygon) != 4:
                continue
            points = polygon.reshape(4, 2).astype("float32")
            ordered = RecognitionEngine._order_points(points)
            top_left, top_right, bottom_right, bottom_left = ordered
            width = int(
                max(
                    np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left)
                )
            )
            height = int(
                max(
                    np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right)
                )
            )
            if width < 200 or height < 120:
                continue
            target = np.array(
                [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32"
            )
            matrix = cv2.getPerspectiveTransform(ordered, target)
            return cv2.warpPerspective(image, matrix, (width, height))
        return image

    @staticmethod
    def _order_points(points: Any) -> Any:
        """把任意顺序的四点转换为左上、右上、右下、左下。"""

        import numpy as np

        ordered = np.zeros((4, 2), dtype="float32")
        sums = points.sum(axis=1)
        differences = np.diff(points, axis=1).reshape(-1)
        ordered[0] = points[np.argmin(sums)]
        ordered[2] = points[np.argmax(sums)]
        ordered[1] = points[np.argmin(differences)]
        ordered[3] = points[np.argmax(differences)]
        return ordered

    @staticmethod
    def _decode_barcodes(prepared: list[PreparedImage]) -> str | None:
        """尝试所有图像分支，优先返回嘉立创结构化二维码。

        袋贴同时存在一维码与二维码时，ZXing 的返回顺序不是业务优先级。若直接取第一项，
        很容易选择底部物流条码而遗漏包含 ``pc/pm/qty`` 的二维码。
        """

        try:
            import zxingcpp
        except ImportError:
            return None
        fallback: str | None = None
        for branch in prepared:
            for result in zxingcpp.read_barcodes(branch.image):
                if result.text:
                    text = str(result.text)
                    if text.startswith("{") and "pc:" in text and "qty:" in text:
                        return text
                    fallback = fallback or text
        return fallback

    def _load_ocr(self) -> Any:
        """延迟创建 RapidOCR；正式包中的模型必须随 wheel 一起存在。"""

        if self._ocr is None:
            from rapidocr import RapidOCR

            self._ocr = RapidOCR()
            self.model_version = "RapidOCR bundled models / ONNX Runtime"
        return self._ocr

    def _recognize_best(self, prepared: list[PreparedImage]) -> tuple[list[OcrLine], str, Any]:
        """比较三个预处理分支和四个方向，选择可信文字最多的结果。"""

        import cv2

        engine = self._load_ocr()
        best_lines: list[OcrLine] = []
        best_name = "none"
        best_image = prepared[0].image
        best_score = -1.0
        for branch in prepared:
            current = branch.image
            for turns in range(4):
                if turns:
                    current = cv2.rotate(current, cv2.ROTATE_90_CLOCKWISE)
                try:
                    raw = engine(current)
                    lines = self._coerce_rapidocr(raw)
                except Exception:
                    lines = []
                score = sum(line.confidence for line in lines) + min(len(lines), 20) * 0.05
                if score > best_score:
                    best_score = score
                    best_lines = lines
                    best_name = f"{branch.name}-rot{turns * 90}"
                    best_image = current.copy()
        return best_lines, best_name, best_image

    @staticmethod
    def _annotated_png(image: Any, lines: list[OcrLine]) -> bytes | None:
        """在最终 OCR 坐标系中绘制文字框，供界面明确显示识别覆盖范围。

        绿色表示可信度至少 80%，黄色表示需要重点核对。框绘制在矫正且旋转后的标签上，
        因而不会出现把矫正图坐标错误套到原始摄像头画面的情况。
        """

        import cv2
        import numpy as np

        canvas = image.copy()
        for line in lines:
            points = np.asarray(line.box, dtype=np.int32).reshape((-1, 1, 2))
            color = (50, 190, 70) if line.confidence >= 0.8 else (0, 190, 255)
            cv2.polylines(canvas, [points], True, color, 2, cv2.LINE_AA)
        success, encoded = cv2.imencode(".png", canvas)
        return bytes(encoded) if success else None

    @staticmethod
    def _coerce_rapidocr(raw: Any) -> list[OcrLine]:
        """兼容 RapidOCR 2/3 的对象和旧元组返回形式。"""

        candidates = getattr(raw, "txts", None)
        if candidates is not None:
            boxes = getattr(raw, "boxes", [])
            scores = getattr(raw, "scores", [])
            return [
                OcrLine(
                    text=str(text),
                    confidence=float(scores[index]),
                    box=[[float(v) for v in point] for point in boxes[index]],
                )
                for index, text in enumerate(candidates)
            ]
        payload = raw[0] if isinstance(raw, tuple) else raw
        lines: list[OcrLine] = []
        for item in payload or []:
            if len(item) >= 3:
                box, text, score = item[0], item[1], item[2]
                lines.append(
                    OcrLine(
                        text=str(text),
                        confidence=float(score),
                        box=[[float(value) for value in point] for point in box],
                    )
                )
        return lines
