"""服务器主导的移动操作会话与 BOM 取货工作流。"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .bom import score_component
from .database import Database, DomainError, utc_now
from .models import BomInputRow, ComponentCreate, IssueRequest, JlcParameterGroup, ReceiveRequest

API_CAPABILITIES = {
    "api_version": "1.1",
    "api_major": 1,
    "minimum_client_version": "1.0.0",
    "features": ["remote_camera", "camera_stream", "receive_session", "issue_session", "bom_pick"],
    "command_version": 1,
}

REMOTE_CAMERA_TTL = timedelta(minutes=2)
INVENTORY_SESSION_TTL = timedelta(minutes=30)
BOM_PICK_TTL = timedelta(hours=24)


class SessionService:
    """集中处理桌面端和移动端共享的命令、状态与库存副作用。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = threading.RLock()
        self._device_activity: dict[str, tuple[str, datetime]] = {}

    def report_device_activity(self, device_id: str, session_id: str | None) -> None:
        with self._lock:
            if session_id:
                self._device_activity[device_id] = (session_id, datetime.now(UTC))
            else:
                self._device_activity.pop(device_id, None)

    def busy_devices(self) -> dict[str, str]:
        active = self.list_active()
        by_id = {entry["session_id"]: entry for entry in active}
        busy = {}
        for entry in active:
            snapshot = entry["snapshot"]
            if snapshot["kind"] in {"RECEIVE", "ISSUE"}:
                device = snapshot["form"].get("target_device_id") or entry["created_by"]
                busy[device] = snapshot["kind"]
        with self._lock:
            for device, (session_id, seen) in self._device_activity.items():
                if session_id in by_id and datetime.now(UTC) - seen < timedelta(seconds=12):
                    busy[device] = by_id[session_id]["snapshot"]["kind"]
        return busy

    def close_inventory(self, session_id: str) -> None:
        with self._lock:
            current = self.get(session_id)
            if current["state"] != "CANCELLED":
                self.command(session_id, str(uuid.uuid4()), current["revision"],
                             "CANCEL", {}, "desktop")

    def create(self, kind: str, payload: dict[str, Any], actor: str = "desktop") -> dict[str, Any]:
        """创建操作会话；BOM 取货会同时保存需求项，但不扣减库存。"""

        if kind not in {"RECEIVE", "ISSUE", "BOM_PICK"}:
            raise DomainError("VALIDATION_ERROR", "不支持的操作会话类型")
        session_id = str(uuid.uuid4())
        now = utc_now()
        state = "PICKING" if kind == "BOM_PICK" else "DRAFT"
        snapshot: dict[str, Any] = {
            "kind": kind,
            "form": dict(payload),
            "warnings": [],
            "locations": [],
        }
        if kind in {"RECEIVE", "ISSUE"}:
            positions = {item["id"]: item["name"] for item in self.database.list_warehouses()}
            snapshot["locations"] = [
                {**item, "warehouse_name": positions.get(item["warehouse_id"], "")}
                for item in self.database.list_locations()
            ]
        device_id = payload.get("target_device_id") or (actor if actor != "desktop" else None)
        if device_id and kind in {"ISSUE", "RECEIVE"}:
            self.cleanup_stale_sessions()
        with self._lock, self.database.write() as connection:
            if device_id and kind in {"ISSUE", "RECEIVE"}:
                activity = self._device_activity.get(device_id)
                if activity and datetime.now(UTC) - activity[1] < timedelta(seconds=12):
                    active = connection.execute(
                        "SELECT 1 FROM operation_sessions WHERE id=? AND state NOT IN ('COMPLETED','CANCELLED')",
                        (activity[0],),
                    ).fetchone()
                    if active:
                        raise DomainError("CONFLICT", "该安卓设备已有操作正在进行")
                for row in connection.execute(
                    """SELECT created_by,snapshot_json FROM operation_sessions
                    WHERE (state='DRAFT' OR (state='COMPLETED'
                    AND json_extract(snapshot_json, '$.form.camera_stream') = 1))
                    AND kind IN ('ISSUE','RECEIVE')"""
                ):
                    form = json.loads(row["snapshot_json"]).get("form", {})
                    if (form.get("target_device_id") or row["created_by"]) == device_id:
                        raise DomainError("CONFLICT", "该安卓设备已有操作正在进行")
            connection.execute(
                """INSERT INTO operation_sessions(
                id,kind,state,revision,snapshot_json,created_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (session_id, kind, state, 0, json.dumps(snapshot, ensure_ascii=False), actor, now, now),
            )
            if kind == "BOM_PICK":
                lines = list(payload.get("lines") or [])
                if not lines:
                    raise DomainError("VALIDATION_ERROR", "BOM 取货任务至少需要一条需求")
                task_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO bom_pick_tasks(
                    id,session_id,name,source_filename,state,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        task_id,
                        session_id,
                        str(payload.get("name") or "BOM 取货"),
                        payload.get("source_filename"),
                        "PICKING",
                        now,
                    ),
                )
                for line in lines:
                    component_id = str(line.get("component_id") or "")
                    self.database._require(connection, "components", component_id, "元件")
                    preferred_location_id = line.get("location_id")
                    if preferred_location_id:
                        self.database._require(
                            connection, "locations", str(preferred_location_id), "仓库"
                        )
                    quantity = int(line.get("quantity") or 0)
                    if quantity <= 0:
                        raise DomainError("VALIDATION_ERROR", "BOM 需求数量必须为正整数")
                    connection.execute(
                        """INSERT INTO bom_pick_items(
                        id,task_id,row_number,mapping_key,raw_json,planned_component_id,
                        required_quantity,preferred_location_id) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()),
                            task_id,
                            int(line.get("row_number") or 0),
                            line.get("mapping_key"),
                            json.dumps(line.get("raw_input") or {}, ensure_ascii=False),
                            component_id,
                            quantity,
                            preferred_location_id,
                        ),
                    )
        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        """返回客户端唯一可信的规范化会话快照。"""

        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM operation_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "操作会话不存在")
            snapshot = json.loads(row["snapshot_json"])
            if row["kind"] == "BOM_PICK":
                snapshot = self._pick_snapshot(connection, session_id)
            else:
                # 仓库颜色来自当前设置，已打开的会话无需更改库存命令修订号。
                storage = snapshot.get("locations") or []
                batch_storage = (snapshot.get("batch") or {}).get("locations") or []
                if storage or batch_storage:
                    colors = {
                        location["id"]: location["color"]
                        for location in connection.execute("SELECT id,color FROM locations")
                    }
                    for location in storage:
                        location["color"] = colors.get(location["id"])
                    for location in batch_storage:
                        location["color"] = colors.get(location["location_id"])
            result = json.loads(row["result_json"]) if row["result_json"] else None
        return self._envelope(row, snapshot, result)

    def list_active(self, *, inventory_only: bool = False) -> list[dict[str, Any]]:
        """列出手机可继续操作的未结束会话。"""

        self.cleanup_stale_sessions()
        with self.database.read() as connection:
            ids = [
                str(row[0])
                for row in connection.execute(
                    """SELECT id FROM operation_sessions
                    WHERE state != 'CANCELLED' AND (state != 'COMPLETED'
                    OR json_extract(snapshot_json, '$.form.camera_stream') = 1)
                    AND (? = 0 OR kind IN ('ISSUE','RECEIVE'))
                    ORDER BY updated_at DESC""",
                    (int(inventory_only),),
                )
            ]
        return [self.get(session_id) for session_id in ids]

    def cleanup_stale_sessions(self, now: datetime | None = None) -> int:
        """取消超过保留时间的移动任务，避免手机端长期堆积失效会话。"""

        current_time = now or datetime.now(UTC)
        expired: list[tuple[str, str]] = []
        with self._lock, self.database.write() as connection:
            rows = connection.execute(
                """SELECT id,kind,snapshot_json,updated_at,created_by FROM operation_sessions
                WHERE state != 'CANCELLED' AND (state != 'COMPLETED'
                OR json_extract(snapshot_json, '$.form.camera_stream') = 1)"""
            ).fetchall()
            for row in rows:
                snapshot = json.loads(row["snapshot_json"])
                form = snapshot.get("form") or {}
                camera_only = bool(form.get("camera_only"))
                device = form.get("target_device_id") or row["created_by"]
                activity = self._device_activity.get(device)
                if activity and activity[0] == row["id"] and current_time - activity[1] < timedelta(seconds=12):
                    continue
                ttl = (
                    REMOTE_CAMERA_TTL
                    if camera_only
                    else BOM_PICK_TTL
                    if row["kind"] == "BOM_PICK"
                    else INVENTORY_SESSION_TTL
                )
                updated_at = datetime.fromisoformat(
                    str(row["updated_at"]).replace("Z", "+00:00")
                )
                if current_time - updated_at > ttl:
                    expired.append((str(row["id"]), str(row["kind"])))
            if expired:
                timestamp = utc_now()
                connection.executemany(
                    """UPDATE operation_sessions SET state='CANCELLED',revision=revision+1,
                    updated_at=? WHERE id=?""",
                    [(timestamp, session_id) for session_id, _kind in expired],
                )
                connection.executemany(
                    "UPDATE bom_pick_tasks SET state='CANCELLED' WHERE session_id=?",
                    [(session_id,) for session_id, kind in expired if kind == "BOM_PICK"],
                )
        return len(expired)

    def command(
        self,
        session_id: str,
        command_id: str,
        expected_revision: int,
        command: str,
        payload: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        """串行执行命令，并持久返回相同 ``command_id`` 的首次响应。"""

        with self._lock:
            cached = self._cached_command(session_id, command_id)
            if cached is not None:
                return cached
            current = self.get(session_id)
            if int(current["revision"]) != expected_revision:
                raise DomainError("REVISION_CONFLICT", "会话已被另一端更新，请刷新后重试")
            kind = str(current["snapshot"]["kind"])
            if kind == "BOM_PICK":
                response = self._pick_command(session_id, command, payload, actor)
            else:
                response = self._inventory_command(session_id, kind, command, payload, actor)
            self._record_command(session_id, command_id, response)
            return response

    def apply_recognition(self, session_id: str, draft: dict[str, Any]) -> dict[str, Any]:
        """把服务端 OCR 草稿写回入库会话，并推进修订号。"""

        with self._lock:
            current = self.get(session_id)
            if current["state"] == "CANCELLED" or (
                current["state"] == "COMPLETED"
                and not current["snapshot"].get("form", {}).get("camera_stream")
            ):
                raise DomainError("CONFLICT", "操作会话已关闭")
            if current["snapshot"]["kind"] != "RECEIVE":
                raise DomainError("VALIDATION_ERROR", "只有入库会话可以接收标签图片")
            snapshot = current["snapshot"]
            previous = snapshot.get("form", {})
            snapshot["form"] = {
                key: previous[key]
                for key in ("camera_only", "camera_stream", "target_device_id", "location_id")
                if key in previous
            }
            snapshot["draft"] = draft
            snapshot["warnings"] = list(draft.get("warnings") or [])
            values = {
                key: str(value.get("normalized_value") or "")
                for key, value in (draft.get("fields") or {}).items()
            }
            component = snapshot.setdefault("form", {}).setdefault("component", {})
            component.update(
                {
                    "name": values.get("description") or values.get("mpn") or "",
                    "lcsc_part_number": values.get("lcsc_part_number") or None,
                    "manufacturer_part_number": values.get("manufacturer_part_number")
                    or values.get("mpn")
                    or None,
                    "brand": values.get("brand") or None,
                    "package": values.get("package") or None,
                }
            )
            form = snapshot["form"]
            if draft.get("raw_barcode"):
                form["batch_metadata"] = {"raw_barcode": draft["raw_barcode"]}
            for key in ("batch_code", "order_number"):
                if values.get(key):
                    form[key] = values[key]
            if values.get("quantity", "").isdigit():
                form["quantity"] = int(values["quantity"])
            lcsc = values.get("lcsc_part_number")
            query = lcsc or values.get("mpn") or values.get("description")
            candidates = self.database.list_components(query) if query else []
            snapshot["component_candidates"] = candidates[:30]
            if lcsc:
                exact = next(
                    (
                        item
                        for item in candidates
                        if str(item.get("lcsc_part_number") or "").casefold() == lcsc.casefold()
                    ),
                    None,
                )
                if exact:
                    form["component_id"] = exact["id"]
            return self._save_snapshot(session_id, "DRAFT", snapshot, None)

    def _inventory_command(
        self, session_id: str, kind: str, command: str, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        current = self.get(session_id)
        snapshot = current["snapshot"]
        form = snapshot.setdefault("form", {})
        if command == "CANCEL":
            if current["state"] == "COMPLETED" and not form.get("camera_stream"):
                return current
            return self._save_snapshot(session_id, "CANCELLED", snapshot, None)
        if current["state"] == "CANCELLED":
            raise DomainError("CONFLICT", "操作会话已关闭")
        if current["state"] == "COMPLETED" and not (
            form.get("camera_stream") and command == "SCAN_CODE"
        ):
            return current
        if command == "UPDATE_AND_CONFIRM":
            form.update({key: value for key, value in payload.items()
                         if key not in {"camera_only", "camera_stream", "target_device_id"}})
            command = "CONFIRM"
        if command == "UPDATE":
            form.update({key: value for key, value in payload.items()
                         if key not in {"camera_only", "camera_stream", "target_device_id"}})
            return self._save_snapshot(session_id, "DRAFT", snapshot, None)
        if command == "SELECT_ISSUE_LOCATION" and kind == "ISSUE" and snapshot.get("batch"):
            location = payload.get("location_id")
            if location is not None and not any(
                item["location_id"] == location and item["remaining_quantity"] > 0
                for item in snapshot["batch"]["locations"]
            ):
                raise DomainError("VALIDATION_ERROR", "所选仓库没有该批次库存")
            self.database.remember_issue_location(location)
            form["location_id"] = location
            return self._save_snapshot(session_id, "DRAFT", snapshot, None)
        if command == "SCAN_CODE" and kind == "ISSUE":
            batch = self.database.resolve_batch_code(str(payload.get("raw_text") or ""))
            snapshot["batch"] = batch
            snapshot["component"] = self.database.get_component(batch["component_id"])
            preferred = self.database.last_issue_location()
            form.update(
                component_id=batch["component_id"],
                batch_id=batch["id"],
                quantity=1,
                location_id=next((
                    item["location_id"] for item in batch["locations"]
                    if item["location_id"] == preferred and item["remaining_quantity"] > 0
                ), None),
                notes=None,
            )
            return self._save_snapshot(session_id, "DRAFT", snapshot, None)
        if command != "CONFIRM":
            raise DomainError("VALIDATION_ERROR", "当前会话不支持该命令")
        platform = "PC" if actor == "desktop" else "安卓"
        if kind == "RECEIVE":
            if snapshot.get("draft") and not form.get("source"):
                form["source"] = "扫码入库"
            component_id = form.get("component_id")
            groups = [JlcParameterGroup.model_validate(group) for group in form.get("parameter_groups", [])]
            if not component_id:
                component = self.database.create_component(
                    ComponentCreate.model_validate(form.get("component") or {})
                )
                component_id = component["id"]
                form["component_id"] = component_id
            elif "parameter_groups" in form:
                self.database.update_component(component_id, ComponentCreate.model_validate(form["component"]))
            if "parameter_groups" in form:
                self.database.save_component_parameter_groups(component_id, groups)
            result = self.database.receive(
                ReceiveRequest.model_validate({**form, "component_id": component_id}),
                platform=platform,
            )
        else:
            if form.get("camera_stream") and not snapshot.get("batch"):
                raise DomainError("VALIDATION_ERROR", "请先扫描有效批次")
            if snapshot.get("batch"):
                form.update(batch_id=snapshot["batch"]["id"],
                            component_id=snapshot["batch"]["component_id"])
                if not form.get("source"):
                    form["source"] = "扫码出库"
            result = self.database.issue(IssueRequest.model_validate(form), platform=platform)
            if form.get("batch_id"):
                snapshot["batch"] = self.database.get_batch(str(form["batch_id"]))
        return self._save_snapshot(session_id, "COMPLETED", snapshot, result)

    def _pick_command(
        self, session_id: str, command: str, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        if command == "CANCEL":
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE bom_pick_tasks SET state='CANCELLED' WHERE session_id=?", (session_id,)
                )
            return self._set_session_state(session_id, "CANCELLED")
        if command == "UNDO":
            with self.database.write() as connection:
                entry_id = str(payload.get("entry_id") or "")
                deleted = connection.execute(
                    """DELETE FROM bom_pick_entries WHERE id=? AND item_id IN(
                    SELECT i.id FROM bom_pick_items i JOIN bom_pick_tasks t ON t.id=i.task_id
                    WHERE t.session_id=? AND t.state IN ('PICKING','READY','BLOCKED'))""",
                    (entry_id, session_id),
                ).rowcount
                if not deleted:
                    raise DomainError("NOT_FOUND", "可撤销的取货记录不存在")
            return self._refresh_pick_state(session_id)
        if command == "CHECK_ITEM":
            self._manual_pick(session_id, str(payload.get("item_id") or ""), actor)
            return self._refresh_pick_state(session_id)
        if command == "SCAN_CODE":
            return self._scan_pick(session_id, payload, actor)
        if command == "CONFIRM_SUBSTITUTE":
            self._confirm_substitute(session_id, payload, actor)
            return self._refresh_pick_state(session_id)
        if command == "COMMIT":
            return self._commit_pick(session_id, actor)
        raise DomainError("VALIDATION_ERROR", "当前取货任务不支持该命令")

    def _scan_pick(self, session_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        batch = self.database.resolve_batch_code(str(payload.get("raw_text") or ""))
        quantity = int(payload.get("quantity") or 0)
        if quantity <= 0 or quantity > int(batch["remaining_quantity"]):
            raise DomainError("VALIDATION_ERROR", "取货数量必须为正且不能超过批次余量")
        snapshot = self.get(session_id)["snapshot"]
        item_id = payload.get("item_id")
        direct = [
            item
            for group in snapshot["groups"]
            for item in group["items"]
            if item["planned_component_id"] == batch["component_id"] and item["remaining"] > 0
        ]
        target = next(
            (item for item in direct if item["id"] == item_id), direct[0] if direct else None
        )
        selected_location = payload.get("location_id")
        preferred_location = target.get("preferred_location_id") if target else None
        if selected_location and preferred_location and selected_location != preferred_location:
            raise DomainError("VALIDATION_ERROR", "扫码仓库与该 BOM 行指定仓库不一致")
        if selected_location:
            location = next(
                (
                    item
                    for item in batch["locations"]
                    if item["location_id"] == selected_location
                ),
                None,
            )
            if location is None or quantity > int(location["remaining_quantity"]):
                raise DomainError("VALIDATION_ERROR", "所选仓库没有足够的该批次库存")
            batch["location_id"] = selected_location
        if target:
            if preferred_location:
                preferred = next(
                    (
                        location
                        for location in batch.get("locations", [])
                        if location["location_id"] == preferred_location
                        and int(location["remaining_quantity"]) >= quantity
                    ),
                    None,
                )
                if preferred is None:
                    raise DomainError("INSUFFICIENT_STOCK", "指定仓库没有足够的该批次库存")
                batch["location_id"] = preferred_location
            if not batch.get("location_id"):
                suggested_locations = {
                    suggestion["location_id"] for suggestion in target.get("suggestions", [])
                }
                matched_location = next(
                    (
                        location
                        for location in batch["locations"]
                        if location["location_id"] in suggested_locations
                        and int(location["remaining_quantity"]) >= quantity
                    ),
                    None,
                )
                if matched_location:
                    batch["location_id"] = matched_location["location_id"]
            self._insert_pick_entry(target, batch, quantity, "SCAN", None, actor)
            return self._refresh_pick_state(session_id)
        candidates = self._substitute_candidates(snapshot, batch)
        snapshot["pending_substitution"] = {
            "batch": batch,
            "quantity": quantity,
            "candidates": candidates,
        }
        if not candidates:
            snapshot["warnings"] = ["扫描物料不在当前需求中，也没有可靠的替代候选"]
        return self._save_snapshot(session_id, self.get(session_id)["state"], snapshot, None)

    def _confirm_substitute(
        self, session_id: str, payload: dict[str, Any], actor: str
    ) -> None:
        snapshot = self.get(session_id)["snapshot"]
        pending = snapshot.get("pending_substitution") or {}
        batch = pending.get("batch")
        if not batch:
            raise DomainError("VALIDATION_ERROR", "当前没有待确认的替代料")
        item_id = str(payload.get("item_id") or "")
        item = next(
            (
                item
                for group in snapshot["groups"]
                for item in group["items"]
                if item["id"] == item_id
            ),
            None,
        )
        if item is None:
            raise DomainError("NOT_FOUND", "替代目标需求不存在")
        quantity = int(payload.get("quantity") or pending.get("quantity") or 0)
        reason = str(payload.get("reason") or "用户确认替代")
        if not batch.get("location_id"):
            preferred_location = item.get("preferred_location_id")
            location = next(
                (
                    entry
                    for entry in batch.get("locations", [])
                    if (not preferred_location or entry["location_id"] == preferred_location)
                    if int(entry["remaining_quantity"]) >= quantity
                ),
                None,
            )
            if location:
                batch["location_id"] = location["location_id"]
        self._insert_pick_entry(item, batch, quantity, "SUBSTITUTE", reason, actor)
        if payload.get("remember") and item.get("mapping_key"):
            with self.database.write() as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO bom_substitution_mappings(
                    id,mapping_key,substitute_component_id,reason,created_at) VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), item["mapping_key"], batch["component_id"], reason, utc_now()),
                )

    def _manual_pick(self, session_id: str, item_id: str, actor: str) -> None:
        snapshot = self.get(session_id)["snapshot"]
        item = next(
            (item for group in snapshot["groups"] for item in group["items"] if item["id"] == item_id),
            None,
        )
        if item is None:
            raise DomainError("NOT_FOUND", "取货项目不存在")
        remaining = int(item["remaining"])
        if remaining <= 0:
            return
        suggestions = list(item.get("suggestions") or [])
        if sum(int(entry["quantity"]) for entry in suggestions) < remaining:
            raise DomainError("INSUFFICIENT_STOCK", "当前建议库存不足，无法整项勾选")
        for suggestion in suggestions:
            if remaining <= 0:
                break
            quantity = min(remaining, int(suggestion["quantity"]))
            self._insert_pick_entry(item, suggestion, quantity, "CHECK", None, actor)
            remaining -= quantity

    def _insert_pick_entry(
        self,
        item: dict[str, Any],
        source: dict[str, Any],
        quantity: int,
        method: str,
        reason: str | None,
        actor: str,
    ) -> None:
        if quantity <= 0 or quantity > int(item["remaining"]):
            raise DomainError("VALIDATION_ERROR", "取货数量超过当前需求余量")
        location_id = source.get("location_id")
        if not location_id:
            locations = [entry for entry in source.get("locations", []) if int(entry["remaining_quantity"]) > 0]
            if len(locations) != 1:
                raise DomainError("VALIDATION_ERROR", "请先选择具体仓库")
            location_id = locations[0]["location_id"]
        with self.database.write() as connection:
            connection.execute(
                """INSERT INTO bom_pick_entries(
                id,item_id,actual_component_id,location_id,batch_id,quantity,method,
                substitute_reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    item["id"],
                    source.get("component_id") or item["planned_component_id"],
                    location_id,
                    source.get("id") or source.get("batch_id"),
                    quantity,
                    method,
                    reason,
                    actor,
                    utc_now(),
                ),
            )

    def _substitute_candidates(
        self, snapshot: dict[str, Any], batch: dict[str, Any]
    ) -> list[dict[str, Any]]:
        component = self.database.get_component(str(batch["component_id"]))
        candidates: list[dict[str, Any]] = []
        with self.database.read() as connection:
            remembered = {
                row["mapping_key"]
                for row in connection.execute(
                    """SELECT mapping_key FROM bom_substitution_mappings
                    WHERE substitute_component_id=?""",
                    (batch["component_id"],),
                )
            }
        for group in snapshot["groups"]:
            for item in group["items"]:
                if item["remaining"] <= 0:
                    continue
                row = BomInputRow.model_validate(item["raw_input"])
                score, reason = score_component(component, row)
                if item.get("mapping_key") in remembered:
                    score, reason = 100, "已保存的替代料映射"
                if score >= 50:
                    candidates.append(
                        {"item_id": item["id"], "score": score, "reason": reason, "item": item}
                    )
        return sorted(candidates, key=lambda value: -int(value["score"]))[:5]

    def _commit_pick(self, session_id: str, actor: str) -> dict[str, Any]:
        """原子提交取货任务；库存变化导致失败时持久标记为阻塞。"""

        try:
            return self._commit_pick_once(session_id, actor)
        except DomainError:
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE bom_pick_tasks SET state='BLOCKED' WHERE session_id=?",
                    (session_id,),
                )
                connection.execute(
                    """UPDATE operation_sessions SET state='BLOCKED',revision=revision+1,
                    updated_at=? WHERE id=?""",
                    (utc_now(), session_id),
                )
            raise

    def _commit_pick_once(self, session_id: str, actor: str) -> dict[str, Any]:
        with self.database.write() as connection:
            task = connection.execute(
                "SELECT * FROM bom_pick_tasks WHERE session_id=?", (session_id,)
            ).fetchone()
            if task is None:
                raise DomainError("NOT_FOUND", "BOM 取货任务不存在")
            if task["state"] == "COMPLETED":
                return self.get(session_id)
            items = connection.execute(
                "SELECT * FROM bom_pick_items WHERE task_id=? ORDER BY row_number", (task["id"],)
            ).fetchall()
            for item in items:
                picked = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM bom_pick_entries WHERE item_id=?",
                    (item["id"],),
                ).fetchone()[0]
                if int(picked) != int(item["required_quantity"]):
                    raise DomainError("VALIDATION_ERROR", "所有需求完成后才能提交出库")
            bom_id = str(uuid.uuid4())
            group_id = str(uuid.uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO boms(id,name,source_filename,created_at) VALUES(?,?,?,?)",
                (bom_id, task["name"], task["source_filename"], now),
            )
            affected = 0
            try:
                for item in items:
                    entries = connection.execute(
                        "SELECT * FROM bom_pick_entries WHERE item_id=? ORDER BY created_at,id",
                        (item["id"],),
                    ).fetchall()
                    actual = [dict(entry) for entry in entries]
                    connection.execute(
                        """INSERT INTO bom_lines(
                        id,bom_id,row_number,raw_json,component_id,quantity) VALUES(?,?,?,?,?,?)""",
                        (
                            str(uuid.uuid4()),
                            bom_id,
                            item["row_number"],
                            json.dumps(
                                {
                                    "input": json.loads(item["raw_json"]),
                                    "mapping_key": item["mapping_key"],
                                    "pick_entries": actual,
                                },
                                ensure_ascii=False,
                            ),
                            item["planned_component_id"],
                            item["required_quantity"],
                        ),
                    )
                    for entry in entries:
                        affected += self.database._issue_from_lots(
                            connection,
                            group_id,
                            entry["actual_component_id"],
                            entry["location_id"],
                            int(entry["quantity"]),
                            f"BOM 取货: {task['name']}",
                            entry["substitute_reason"],
                            "BOM_PICK_ISSUE",
                            entry["batch_id"],
                            platform="PC" if actor == "desktop" else "安卓",
                        )
            except DomainError:
                connection.execute(
                    "UPDATE bom_pick_tasks SET state='BLOCKED' WHERE id=?", (task["id"],)
                )
                raise
            result = {
                "bom_id": bom_id,
                "transaction_group_id": group_id,
                "affected_transactions": affected,
            }
            connection.execute(
                """UPDATE bom_pick_tasks SET state='COMPLETED',transaction_group_id=?,
                completed_at=? WHERE id=?""",
                (group_id, now, task["id"]),
            )
            connection.execute(
                """UPDATE operation_sessions SET state='COMPLETED',revision=revision+1,
                result_json=?,updated_at=? WHERE id=?""",
                (json.dumps(result, ensure_ascii=False), now, session_id),
            )
        return self.get(session_id)

    def _pick_snapshot(self, connection: Any, session_id: str) -> dict[str, Any]:
        task = connection.execute(
            "SELECT * FROM bom_pick_tasks WHERE session_id=?", (session_id,)
        ).fetchone()
        if task is None:
            raise DomainError("NOT_FOUND", "BOM 取货任务不存在")
        rows = connection.execute(
            """SELECT i.*,c.name component_name,c.package,c.category
            FROM bom_pick_items i JOIN components c ON c.id=i.planned_component_id
            WHERE i.task_id=? ORDER BY i.row_number""",
            (task["id"],),
        ).fetchall()
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            entries = [
                dict(entry)
                for entry in connection.execute(
                    "SELECT * FROM bom_pick_entries WHERE item_id=? ORDER BY created_at,id", (row["id"],)
                )
            ]
            picked = sum(int(entry["quantity"]) for entry in entries)
            suggestions = self._lot_suggestions(
                connection,
                row["planned_component_id"],
                int(row["required_quantity"]) - picked,
                row["preferred_location_id"],
            )
            primary = suggestions[0] if suggestions else {
                "warehouse_id": "unallocated",
                "warehouse_name": "未分配",
                "location_id": "unallocated",
                "location_name": "库存不足",
            }
            key = (str(primary["warehouse_id"]), str(primary["location_id"]))
            group = groups.setdefault(
                key,
                {
                    "warehouse_id": primary["warehouse_id"],
                    "warehouse_name": primary["warehouse_name"],
                    "location_id": primary["location_id"],
                    "location_name": primary["location_name"],
                    "color": primary.get("color"),
                    "items": [],
                },
            )
            group["items"].append(
                {
                    "id": row["id"],
                    "row_number": row["row_number"],
                    "mapping_key": row["mapping_key"],
                    "raw_input": json.loads(row["raw_json"]),
                    "planned_component_id": row["planned_component_id"],
                    "preferred_location_id": row["preferred_location_id"],
                    "component_name": row["component_name"],
                    "package": row["package"],
                    "required_quantity": int(row["required_quantity"]),
                    "picked_quantity": picked,
                    "remaining": max(0, int(row["required_quantity"]) - picked),
                    "entries": entries,
                    "suggestions": suggestions,
                }
            )
        stored = connection.execute(
            "SELECT snapshot_json FROM operation_sessions WHERE id=?", (session_id,)
        ).fetchone()
        transient = json.loads(stored[0]) if stored else {}
        return {
            "kind": "BOM_PICK",
            "task_id": task["id"],
            "name": task["name"],
            "groups": list(groups.values()),
            "pending_substitution": transient.get("pending_substitution"),
            "warnings": transient.get("warnings", []),
        }

    @staticmethod
    def _lot_suggestions(
        connection: Any,
        component_id: str,
        needed: int,
        preferred_location_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT s.batch_id,s.location_id,s.remaining_quantity,l.name location_name,
            w.id warehouse_id,w.name warehouse_name,l.color,b.custom_code,b.order_number
            FROM stock_lots s JOIN locations l ON l.id=s.location_id
            JOIN warehouses w ON w.id=l.warehouse_id
            LEFT JOIN inventory_batches b ON b.id=s.batch_id
            LEFT JOIN storage_location_priorities p ON p.location_id=l.id
            WHERE s.component_id=? AND s.remaining_quantity>0
              AND (? IS NULL OR s.location_id=?)
            ORDER BY COALESCE(p.priority,2147483647),w.name,l.name,s.received_at,s.id""",
            (component_id, preferred_location_id, preferred_location_id),
        ).fetchall()
        result: list[dict[str, Any]] = []
        remaining = max(0, needed)
        for row in rows:
            if remaining <= 0:
                break
            quantity = min(remaining, int(row["remaining_quantity"]))
            value = dict(row)
            value["quantity"] = quantity
            value["component_id"] = component_id
            value["batch_id"] = value.pop("batch_id")
            result.append(value)
            remaining -= quantity
        return result

    def _refresh_pick_state(self, session_id: str) -> dict[str, Any]:
        snapshot = self.get(session_id)["snapshot"]
        snapshot.pop("pending_substitution", None)
        snapshot["warnings"] = []
        ready = all(
            item["remaining"] == 0 for group in snapshot["groups"] for item in group["items"]
        )
        state = "READY" if ready else "PICKING"
        with self.database.write() as connection:
            connection.execute(
                "UPDATE bom_pick_tasks SET state=? WHERE session_id=?", (state, session_id)
            )
        return self._save_snapshot(session_id, state, snapshot, None)

    def _set_session_state(self, session_id: str, state: str) -> dict[str, Any]:
        current = self.get(session_id)
        return self._save_snapshot(session_id, state, current["snapshot"], current["action_result"])

    def _save_snapshot(
        self, session_id: str, state: str, snapshot: dict[str, Any], result: dict[str, Any] | None
    ) -> dict[str, Any]:
        with self.database.write() as connection:
            connection.execute(
                """UPDATE operation_sessions SET state=?,revision=revision+1,snapshot_json=?,
                result_json=COALESCE(?,result_json),updated_at=? WHERE id=?""",
                (
                    state,
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    utc_now(),
                    session_id,
                ),
            )
        return self.get(session_id)

    def _cached_command(self, session_id: str, command_id: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT response_json FROM session_commands WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def _record_command(self, session_id: str, command_id: str, response: dict[str, Any]) -> None:
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO session_commands VALUES(?,?,?,?)",
                (session_id, command_id, json.dumps(response, ensure_ascii=False), utc_now()),
            )

    @staticmethod
    def _envelope(row: Any, snapshot: dict[str, Any], result: Any) -> dict[str, Any]:
        return {
            "session_id": row["id"],
            "created_by": row["created_by"],
            "revision": int(row["revision"]),
            "state": row["state"],
            "snapshot": snapshot,
            "action_result": result,
            "warnings": list(snapshot.get("warnings") or []),
            "capabilities": API_CAPABILITIES,
        }
