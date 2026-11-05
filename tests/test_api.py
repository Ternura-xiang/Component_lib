"""本机 API 认证、契约与幂等测试。"""

import asyncio
from pathlib import Path

import httpx

from component_library.api import ApiContext, create_api
from component_library.database import Database
from component_library.recognition import RecognitionEngine


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
