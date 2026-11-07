"""二维码实时识别回归测试。"""

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import zxingcpp
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame

from component_library.database import Database
from component_library.models import LabelDraft
from component_library.recognition import PreparedImage, RecognitionEngine
from component_library.ui.main_window import MainWindow, ScanPage


def test_decode_small_jlc_qr_from_camera_frame() -> None:
    """高分辨率相机画面中占比较小的立创码仍可被识别。"""

    payload = (
        "{on:SO26090420076,pc:C270654,pm:0402WGF1072TCE,"
        "qty:100,mc:,cc:1,pdi:234895663,hp:12}"
    )
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    qr_image = np.asarray(zxingcpp.write_barcode_to_image(barcode, size_hint=400))
    qr_image = cv2.resize(qr_image, (80, 80), interpolation=cv2.INTER_AREA)
    frame = np.full((1080, 1920, 3), 190, dtype=np.uint8)
    frame[500:580, 920:1000] = cv2.cvtColor(qr_image, cv2.COLOR_GRAY2BGR)
    encoded, image_bytes = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])

    assert encoded
    assert RecognitionEngine().decode_barcode(image_bytes.tobytes()) == payload


def test_realtime_decode_ignores_logistics_one_dimensional_barcode() -> None:
    """出库扫描不得把袋贴底部物流一维码误当成批次二维码。"""

    barcode = zxingcpp.create_barcode("3986327", zxingcpp.BarcodeFormat.Code128)
    barcode_image = np.asarray(
        zxingcpp.write_barcode_to_image(barcode, size_hint=500, with_hrt=True)
    )
    encoded, image_bytes = cv2.imencode(".jpg", barcode_image)

    assert encoded
    assert RecognitionEngine().decode_barcode(image_bytes.tobytes()) is None


def test_receive_decode_uses_zxing_default_formats_without_explicit_none() -> None:
    """入库识别不显式传递 formats=None，兼容 zxing-cpp 2.3 绑定。"""

    barcode = zxingcpp.create_barcode("3986327", zxingcpp.BarcodeFormat.Code128)
    barcode_image = np.asarray(zxingcpp.write_barcode_to_image(barcode, size_hint=500))

    assert RecognitionEngine._decode_barcodes(
        [PreparedImage("receive", barcode_image)]
    ) == "3986327"


def test_realtime_decode_prefers_qr_when_label_also_contains_barcode() -> None:
    """同一袋贴包含二维码和一维码时只返回二维码内容。"""

    payload = "{on:SO26090420180,pc:C22356447,pm:RCA03120KFLF,qty:100}"
    qr = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    qr_image = np.asarray(zxingcpp.write_barcode_to_image(qr, size_hint=300))
    linear = zxingcpp.create_barcode("3986327", zxingcpp.BarcodeFormat.Code128)
    linear_image = np.asarray(zxingcpp.write_barcode_to_image(linear, size_hint=500))
    frame = np.full((1000, 900), 255, dtype=np.uint8)
    frame[40 : 40 + qr_image.shape[0], 80 : 80 + qr_image.shape[1]] = qr_image
    frame[
        650 : 650 + linear_image.shape[0],
        80 : 80 + linear_image.shape[1],
    ] = linear_image
    encoded, image_bytes = cv2.imencode(".jpg", frame)

    assert encoded
    assert RecognitionEngine().decode_barcode(image_bytes.tobytes()) == payload


def test_qimage_camera_frame_encodes_and_decodes_qr() -> None:
    """PySide6 摄像头 QImage 可编码成 PNG 并交给实时解码器。"""

    payload = "{on:SO26090420180,pc:C22356447,pm:RCA03120KFLF,qty:100}"
    qr = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    qr_image = np.asarray(zxingcpp.write_barcode_to_image(qr, size_hint=400))
    encoded, png_bytes = cv2.imencode(".png", qr_image)
    assert encoded
    frame = QImage.fromData(png_bytes.tobytes(), "PNG")

    camera_bytes = ScanPage._image_bytes(frame)

    assert camera_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert RecognitionEngine().decode_barcode(camera_bytes) == payload


def test_issue_page_video_frame_triggers_qr_result(
    qtbot: Any, tmp_path: Path
) -> None:
    """真实 QVideoFrame 从页面回调进入后台线程后能触发扫码结果。"""

    payload = "{on:SO26090420180,pc:C22356447,pm:RCA03120KFLF,qty:100}"
    qr = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    qr_image = np.asarray(zxingcpp.write_barcode_to_image(qr, size_hint=500))
    encoded, png_bytes = cv2.imencode(".png", qr_image)
    assert encoded
    image = QImage.fromData(png_bytes.tobytes(), "PNG")
    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)

    window.issue_page._scan_frame(QVideoFrame(image))
    qtbot.waitUntil(lambda: window.issue_page.scan_text.text() == payload, timeout=5_000)

    assert window.issue_page.scan_attempts == 1
    assert "二维码识别成功" in window.issue_page.scan_status.text()


def test_issue_page_continues_after_empty_frame(
    qtbot: Any, tmp_path: Path
) -> None:
    """未找到二维码的后台任务完成后，后续帧仍能继续识别。"""

    image = QImage(640, 480, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    frame = QVideoFrame(image)
    window = MainWindow(Database(tmp_path / "inventory.db"), RecognitionEngine(), "token")
    qtbot.addWidget(window)
    issue = window.issue_page

    issue._scan_frame(frame)
    qtbot.waitUntil(lambda: issue.scan_worker is None, timeout=5_000)
    issue.last_scan_at = 0.0
    issue._scan_frame(frame)
    qtbot.waitUntil(lambda: issue.scan_worker is None, timeout=5_000)

    assert issue.scan_attempts == 2
    assert issue.scan_received_frames == 2
    assert not issue.scan_busy
    assert "尚未检测到二维码" in issue.scan_status.text()


def test_receive_worker_releases_busy_state(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """入库 OCR Worker 保持强引用，完成后释放忙碌状态。"""

    recognition = RecognitionEngine()
    monkeypatch.setattr(recognition, "analyze", lambda _image: LabelDraft())
    window = MainWindow(Database(tmp_path / "inventory.db"), recognition, "token")
    qtbot.addWidget(window)
    scan = window.scan_page

    scan._analyze(b"test-image")
    qtbot.waitUntil(lambda: scan.recognition_worker is None, timeout=5_000)

    assert not scan.busy
