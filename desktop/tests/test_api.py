"""本机 API 认证、契约与幂等测试。"""

import asyncio
import json
import socket
import ssl
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from wsproto import ConnectionType, WSConnection
from wsproto.events import Request as UpgradeRequest
from wsproto.events import TextMessage

from component_library.api import ApiContext, ApiServer, create_api
from component_library.connection import ConnectionManager
from component_library.database import Database
from component_library.recognition import RecognitionEngine


def test_api_server_without_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows windowed 产物没有标准流，API 仍须能启动并响应请求。"""

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", None)
        patch.setattr(sys, "stderr", None)
        server = ApiServer(FastAPI(), port=0)
    server.start()
    try:
        port = server._socket.getsockname()[1]
        with httpx.Client(transport=httpx.HTTPTransport(retries=5)) as client:
            response = client.get(f"http://127.0.0.1:{port}/openapi.json")
        assert response.status_code == 200
    finally:
        server.stop()
        server.join(timeout=10)
    assert not server.is_alive()


def test_https_pairing_and_device_heartbeat(tmp_path: Path) -> None:
    """局域网 TLS 服务应能配对独立设备令牌并用于认证。"""

    database = Database(tmp_path / "inventory.db")
    connection = ConnectionManager(database, tmp_path)
    app = create_api(ApiContext(database, "desktop-token", RecognitionEngine(), connection))
    server = ApiServer(
        app,
        port=0,
        ssl_certfile=str(connection.cert_path),
        ssl_keyfile=str(connection.key_path),
    )
    server.start()
    try:
        port = server._socket.getsockname()[1]
        with httpx.Client(
            transport=httpx.HTTPTransport(
                retries=5, verify=ssl.create_default_context(cafile=connection.ca_path)
            )
        ) as client:
            paired = client.post(
                f"https://127.0.0.1:{port}/api/v1/pairing",
                json={
                    "pairing_code": connection.pairing_payload()["pairing_code"],
                    "device_name": "Android 测试机",
                    "device_type": "ANDROID_PHONE",
                    "client_version": "1.0.0",
                },
            )
            assert paired.status_code == 200
            assert paired.json()["ca_pem"]
            heartbeat = client.post(
                f"https://127.0.0.1:{port}/api/v1/devices/heartbeat",
                headers={
                    "Authorization": f"Bearer {paired.json()['device_token']}",
                    "X-Client-Version": "1.0.5",
                },
            )
            assert heartbeat.status_code == 200
            assert heartbeat.json()["capabilities"]["api_major"] == 1
            assert connection.list_devices()[0]["client_version"] == "1.0.5"
            bearer = f"Bearer {paired.json()['device_token']}"
            base = f"https://127.0.0.1:{port}/api/v1/sessions"
            created = client.post(
                base, headers={"Authorization": bearer, "Idempotency-Key": "start-camera"},
                json={"kind": "ISSUE", "payload": {"camera_stream": True}},
            )
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            tls = ssl.create_default_context(cafile=connection.ca_path)
            protocol = WSConnection(ConnectionType.CLIENT)
            with (
                socket.create_connection(("127.0.0.1", port), timeout=5) as tcp,
                tls.wrap_socket(tcp, server_hostname="127.0.0.1") as ws,
            ):
                ws.sendall(protocol.send(UpgradeRequest(
                    host=f"127.0.0.1:{port}", target=f"/api/v1/sessions/{session_id}/events",
                    extra_headers=[(b"authorization", bearer.encode())],
                )))

                def next_snapshot() -> dict[str, object]:
                    while data := ws.recv(65536):
                        protocol.receive_data(data)
                        for event in protocol.events():
                            if isinstance(event, TextMessage):
                                return json.loads(event.data)
                    pytest.fail("WebSocket 未返回会话快照")

                assert next_snapshot()["state"] == "DRAFT"
                response = client.post(
                    f"{base}/{session_id}/commands", headers={"Authorization": bearer},
                    json={"command_id": "close-camera", "expected_revision": 0, "command": "CANCEL"},
                )
                assert response.status_code == 200
                assert next_snapshot()["state"] == "CANCELLED"
    finally:
        server.stop()
        server.join(timeout=10)
    assert not server.is_alive()


def application(tmp_path: Path) -> object:
    """创建使用临时数据库的 ASGI 应用。"""

    context = ApiContext(Database(tmp_path / "inventory.db"), "test-token", RecognitionEngine())
    return create_api(context)


async def request(app: object, method: str, path: str, **kwargs: object) -> httpx.Response:
    """直接通过 ASGI 调用 API，不占用真实端口。"""

    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        return await api.request(method, path, **kwargs)


def test_api_rejects_missing_token(tmp_path: Path) -> None:
    response = asyncio.run(request(application(tmp_path), "GET", "/api/v1/components"))
    assert response.status_code == 401


def test_statistics_api_returns_empty_snapshot(tmp_path: Path) -> None:
    """新数据库的统计接口应返回结构稳定的零值快照。"""

    response = asyncio.run(
        request(
            application(tmp_path),
            "GET",
            "/api/v1/statistics",
            headers={"Authorization": "Bearer test-token"},
        )
    )
    assert response.status_code == 200
    assert response.json() == {
        "component_types": 0,
        "total_quantity": 0,
        "priced_quantity": 0,
        "unpriced_quantity": 0,
        "total_value": 0,
        "by_location": [],
        "by_category": [],
    }


def test_write_requires_idempotency_key(tmp_path: Path) -> None:
    response = asyncio.run(
        request(
            application(tmp_path),
            "POST",
            "/api/v1/components",
            headers={"Authorization": "Bearer test-token"},
            json={"name": "电阻"},
        )
    )
    assert response.status_code == 400


def test_repeated_key_returns_same_response(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = application(tmp_path)
        headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "same-request"}
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            first = await api.post("/api/v1/components", headers=headers, json={"name": "电阻"})
            second = await api.post("/api/v1/components", headers=headers, json={"name": "电阻"})
            assert first.status_code == 200
            assert second.json() == first.json()
            listed = await api.get(
                "/api/v1/components", headers={"Authorization": "Bearer test-token"}
            )
            assert len(listed.json()) == 1

    asyncio.run(scenario())


def test_ocr_status_models_are_local_and_valid(tmp_path: Path) -> None:
    response = asyncio.run(
        request(
            application(tmp_path),
            "GET",
            "/api/v1/ocr/status",
            headers={"Authorization": "Bearer test-token"},
        )
    )
    assert response.status_code == 200
    assert response.json()["available"] is True


def test_batch_resolve_api_is_read_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = application(tmp_path)
        auth = {"Authorization": "Bearer test-token"}
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            warehouse = (
                await api.post(
                    "/api/v1/warehouses",
                    headers={**auth, "Idempotency-Key": "batch-warehouse"},
                    json={"name": "主柜"},
                )
            ).json()
            location = (
                await api.post(
                    "/api/v1/locations",
                    headers={**auth, "Idempotency-Key": "batch-location"},
                    json={"warehouse_id": warehouse["id"], "name": "A01"},
                )
            ).json()
            component = (
                await api.post(
                    "/api/v1/components",
                    headers={**auth, "Idempotency-Key": "batch-component"},
                    json={"name": "测试物料"},
                )
            ).json()
            await api.post(
                "/api/v1/inventory/receive",
                headers={**auth, "Idempotency-Key": "batch-receive"},
                json={
                    "component_id": component["id"],
                    "location_id": location["id"],
                    "quantity": 4,
                    "batch_code": "CL0000000123",
                },
            )
            resolved = await api.post(
                "/api/v1/inventory/batches/resolve",
                headers=auth,
                json={"raw_text": "CL0000000123"},
            )
            assert resolved.status_code == 200
            assert resolved.json()["remaining_quantity"] == 4
            again = await api.post(
                "/api/v1/inventory/batches/resolve",
                headers=auth,
                json={"raw_text": "CL0000000123"},
            )
            assert again.json()["remaining_quantity"] == 4

    asyncio.run(scenario())


def test_storage_priority_api_round_trip(tmp_path: Path) -> None:
    """优先级写接口应持久保存并由读取接口返回。"""

    async def scenario() -> None:
        app = application(tmp_path)
        auth = {"Authorization": "Bearer test-token"}
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
            warehouse = await api.post(
                "/api/v1/warehouses",
                headers={**auth, "Idempotency-Key": "warehouse"},
                json={"name": "电阻1/RLC"},
            )
            location = await api.post(
                "/api/v1/locations",
                headers={**auth, "Idempotency-Key": "location"},
                json={"warehouse_id": warehouse.json()["id"], "name": "R01"},
            )
            saved = await api.put(
                "/api/v1/storage-priorities",
                headers={**auth, "Idempotency-Key": "priority"},
                json={"category": "贴片电阻", "priorities": {location.json()["id"]: 1}},
            )
            assert saved.status_code == 200
            assert saved.json()["locations"][0]["priority"] == 1
            loaded = await api.get(
                "/api/v1/storage-priorities", headers=auth, params={"category": "贴片电阻"}
            )
            assert loaded.status_code == 200
            assert loaded.json()[0]["location_id"] == location.json()["id"]

    asyncio.run(scenario())


def test_session_api_returns_canonical_response_and_revision_conflict(tmp_path: Path) -> None:
    """桌面与手机入口共用相同会话响应，并拒绝旧修订覆盖新状态。"""

    async def scenario() -> None:
        app = application(tmp_path)
        headers = {
            "Authorization": "Bearer test-token",
            "Idempotency-Key": "create-mobile-session",
        }
        created = await request(
            app,
            "POST",
            "/api/v1/sessions",
            headers=headers,
            json={"kind": "ISSUE", "payload": {"quantity": 1}},
        )
        assert created.status_code == 200
        body = created.json()
        assert set(body) == {
            "session_id",
            "created_by",
            "revision",
            "state",
            "snapshot",
            "action_result",
            "warnings",
            "capabilities",
        }
        updated = await request(
            app,
            "POST",
            f"/api/v1/sessions/{body['session_id']}/commands",
            headers={"Authorization": "Bearer test-token"},
            json={
                "command_id": "mobile-update",
                "expected_revision": 0,
                "command": "UPDATE",
                "payload": {"notes": "手机输入"},
            },
        )
        assert updated.json()["revision"] == 1
        stale = await request(
            app,
            "POST",
            f"/api/v1/sessions/{body['session_id']}/commands",
            headers={"Authorization": "Bearer test-token"},
            json={
                "command_id": "desktop-stale",
                "expected_revision": 0,
                "command": "UPDATE",
                "payload": {"notes": "过期输入"},
            },
        )
        assert stale.status_code == 409
        repeated = await request(
            app,
            "POST",
            f"/api/v1/sessions/{body['session_id']}/commands",
            headers={"Authorization": "Bearer test-token"},
            json={
                "command_id": "mobile-update",
                "expected_revision": 0,
                "command": "UPDATE",
                "payload": {"notes": "不会覆盖首次结果"},
            },
        )
        assert repeated.json() == updated.json()

    asyncio.run(scenario())


def test_capabilities_do_not_require_token(tmp_path: Path) -> None:
    response = asyncio.run(request(application(tmp_path), "GET", "/api/v1/capabilities"))
    assert response.status_code == 200
    assert response.json()["api_major"] == 1
