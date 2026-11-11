"""移动操作会话、设备配对与 BOM 取货状态机测试。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from component_library.connection import MDNS_SERVICE_TYPE, ConnectionManager
from component_library.database import Database, DomainError
from component_library.models import (
    ComponentCreate,
    IssueRequest,
    LocationCreate,
    ReceiveRequest,
    WarehouseCreate,
)
from component_library.sessions import SessionService


def test_mdns_service_type_respects_dns_sd_label_limit() -> None:
    """服务类型标签必须满足 DNS-SD 的 15 字节上限。"""

    service_label = MDNS_SERVICE_TYPE.removeprefix("_").split(".", 1)[0]
    assert len(service_label.encode("ascii")) <= 15


def stock_fixture(database: Database, *, second: bool = False) -> dict[str, str]:
    warehouse = database.create_warehouse(WarehouseCreate(name="主柜"))
    location = database.create_location(
        LocationCreate(warehouse_id=warehouse["id"], name="A01")
    )
    component = database.create_component(
        ComponentCreate(name="10k 电阻", specification="10k", package="0603")
    )
    database.receive(
        ReceiveRequest(
            component_id=component["id"],
            location_id=location["id"],
            quantity=10,
            batch_code="CL0000000101",
        )
    )
    result = {"warehouse": warehouse["id"], "location": location["id"], "component": component["id"]}
    if second:
        substitute = database.create_component(
            ComponentCreate(name="10k 替代电阻", specification="10k", package="0603")
        )
        database.receive(
            ReceiveRequest(
                component_id=substitute["id"],
                location_id=location["id"],
                quantity=6,
                batch_code="CL0000000102",
            )
        )
        result["substitute"] = substitute["id"]
    return result


def pick_payload(ids: dict[str, str], quantity: int = 4) -> dict[str, object]:
    return {
        "name": "控制板",
        "source_filename": "board.csv",
        "lines": [
            {
                "row_number": 2,
                "component_id": ids["component"],
                "quantity": quantity,
                "mapping_key": "bom-v2:test-resistor",
                "raw_input": {
                    "row_number": 2,
                    "name": "10k 电阻",
                    "quantity": quantity,
                    "specification": "10k",
                    "footprint": "0603",
                    "raw_fields": {},
                    "errors": [],
                },
            }
        ],
    }


def test_phone_issue_stream_survives_confirmation_and_closes(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database, second=True)
    database.issue(IssueRequest(component_id=ids["component"], location_id=ids["location"], quantity=1))
    service = SessionService(database)
    created = service.create("ISSUE", {"camera_stream": True}, "phone")
    session_id = created["session_id"]
    assert service.busy_devices()["phone"] == "ISSUE"
    with pytest.raises(DomainError, match="已有操作"):
        service.create("RECEIVE", {"target_device_id": "phone"}, "desktop")

    scanned = service.command(session_id, "scan1", 0, "SCAN_CODE", {"raw_text": "CL0000000101"}, "phone")
    assert scanned["snapshot"]["form"]["quantity"] == 1
    assert scanned["snapshot"]["form"]["location_id"] == ids["location"]
    confirmed = service.command(
        session_id, "confirm1", 1, "UPDATE_AND_CONFIRM", {"quantity": 2}, "desktop"
    )
    assert confirmed["state"] == "COMPLETED"
    assert confirmed["snapshot"]["batch"]["remaining_quantity"] == 7
    assert service.list_active()[0]["session_id"] == session_id
    next_scan = service.command(session_id, "scan2", 2, "SCAN_CODE", {"raw_text": "CL0000000102"}, "phone")
    assert next_scan["state"] == "DRAFT"
    assert next_scan["snapshot"]["form"]["quantity"] == 1
    with pytest.raises(DomainError, match="另一端更新"):
        service.command(session_id, "stale", 1, "CONFIRM", {}, "desktop")
    service.close_inventory(session_id)
    assert "phone" not in service.busy_devices()
    with pytest.raises(DomainError, match="已关闭"):
        service.command(session_id, "scan3", 4, "SCAN_CODE", {"raw_text": "CL0000000101"}, "phone")


def test_pick_task_is_idempotent_and_commits_atomically(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database)
    service = SessionService(database)
    created = service.create("BOM_PICK", pick_payload(ids))
    item_id = created["snapshot"]["groups"][0]["items"][0]["id"]

    ready = service.command(created["session_id"], "check-1", 0, "CHECK_ITEM", {"item_id": item_id}, "desktop")
    assert ready["state"] == "READY"
    assert service.command(created["session_id"], "check-1", 0, "CHECK_ITEM", {"item_id": item_id}, "desktop") == ready
    completed = service.command(created["session_id"], "commit-1", 1, "COMMIT", {}, "desktop")

    assert completed["state"] == "COMPLETED"
    assert sum(row["quantity"] for row in database.list_stock()) == 6


def test_pick_task_honors_selected_location(tmp_path: Path) -> None:
    """BOM 行指定仓库后，建议批次和整项打钩只能使用该仓库。"""

    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database)
    second = database.create_location(
        LocationCreate(warehouse_id=ids["warehouse"], name="A02")
    )
    database.receive(
        ReceiveRequest(component_id=ids["component"], location_id=second["id"], quantity=6)
    )
    payload = pick_payload(ids, 4)
    payload["lines"][0]["location_id"] = second["id"]  # type: ignore[index]
    service = SessionService(database)
    created = service.create("BOM_PICK", payload)
    item = created["snapshot"]["groups"][0]["items"][0]
    assert item["preferred_location_id"] == second["id"]
    assert {entry["location_id"] for entry in item["suggestions"]} == {second["id"]}
    ready = service.command(
        created["session_id"], "check-location", 0, "CHECK_ITEM", {"item_id": item["id"]}, "desktop"
    )
    assert ready["state"] == "READY"
    assert ready["snapshot"]["groups"][0]["items"][0]["entries"][0]["location_id"] == second["id"]


def test_session_revision_conflict_does_not_change_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database)
    service = SessionService(database)
    created = service.create("ISSUE", {"component_id": ids["component"], "quantity": 1})
    service.command(created["session_id"], "update", 0, "UPDATE", {"notes": "phone"}, "phone")
    with pytest.raises(DomainError, match="刷新"):
        service.command(created["session_id"], "stale", 0, "UPDATE", {"notes": "desktop"}, "desktop")


def test_stale_remote_camera_session_is_removed_from_android_tasks(tmp_path: Path) -> None:
    """超过两分钟的远程摄像头请求应取消并从手机活动任务中消失。"""

    database = Database(tmp_path / "inventory.db")
    service = SessionService(database)
    created = service.create(
        "RECEIVE",
        {"camera_only": True, "target_device_id": "phone-1"},
        "desktop",
    )
    stale = (datetime.now(UTC) - timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    with database.write() as connection:
        connection.execute(
            "UPDATE operation_sessions SET updated_at=? WHERE id=?",
            (stale, created["session_id"]),
        )
    assert service.cleanup_stale_sessions() == 1
    assert service.get(created["session_id"])["state"] == "CANCELLED"
    assert service.list_active() == []


def test_unrequested_batch_requires_confirmed_substitution(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database, second=True)
    service = SessionService(database)
    created = service.create("BOM_PICK", pick_payload(ids, 3))
    scanned = service.command(
        created["session_id"],
        "scan-substitute",
        0,
        "SCAN_CODE",
        {"raw_text": "CL0000000102", "quantity": 3},
        "phone",
    )
    pending = scanned["snapshot"]["pending_substitution"]
    assert pending["candidates"]
    target = pending["candidates"][0]["item_id"]
    ready = service.command(
        created["session_id"],
        "confirm-substitute",
        1,
        "CONFIRM_SUBSTITUTE",
        {"item_id": target, "quantity": 3, "remember": True, "reason": "规格封装一致"},
        "phone",
    )
    assert ready["state"] == "READY"
    service.command(created["session_id"], "commit-substitute", 2, "COMMIT", {}, "phone")
    substitute_stock = [row for row in database.list_stock() if row["component_id"] == ids["substitute"]]
    assert sum(row["quantity"] for row in substitute_stock) == 3


def test_inventory_change_blocks_pick_without_partial_issue(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    ids = stock_fixture(database)
    service = SessionService(database)
    created = service.create("BOM_PICK", pick_payload(ids, 8))
    item_id = created["snapshot"]["groups"][0]["items"][0]["id"]
    ready = service.command(created["session_id"], "check", 0, "CHECK_ITEM", {"item_id": item_id}, "desktop")
    database.issue(IssueRequest(component_id=ids["component"], quantity=5))
    with pytest.raises(DomainError, match="库存不足"):
        service.command(created["session_id"], "commit", ready["revision"], "COMMIT", {}, "desktop")
    assert service.get(created["session_id"])["state"] == "BLOCKED"
    assert sum(row["quantity"] for row in database.list_stock()) == 5


def test_pairing_code_is_single_use_and_device_can_be_deleted(tmp_path: Path) -> None:
    database = Database(tmp_path / "inventory.db")
    manager = ConnectionManager(database, tmp_path)
    payload = manager.pairing_payload()
    assert payload["protocol"] == 2
    assert "ca_pem" not in payload
    assert len(json.dumps(payload, separators=(",", ":"))) < 1024
    assert len(payload["server_cert_sha256"]) == 64
    code = payload["pairing_code"]
    paired = manager.pair(str(code), "测试手机", "ANDROID_PHONE", "1.0.0")
    assert paired["ca_pem"]
    assert manager.authenticate(paired["device_token"]) == paired["device_id"]
    listed = manager.list_devices()
    assert listed[0]["online"] is True
    assert manager.available_android_devices()[0]["id"] == paired["device_id"]
    with pytest.raises(DomainError, match="无效"):
        manager.pair(str(code), "另一手机", "ANDROID_PHONE", "1.0.0")
    manager.delete(paired["device_id"])
    assert manager.authenticate(paired["device_token"]) is None
    assert manager.list_devices() == []
