"""独立的立创与用户元件资料库及其可移植文件格式。"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

Catalog = Literal["marketplace", "user"]
CATALOGS: tuple[Catalog, ...] = ("marketplace", "user")
SNAPSHOT_TABLE = "component_catalog_backup"


def _category_key(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


@cache
def _jlc_category_index() -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    # 分类数据来自立创商城元器件目录页面，只包含目录名称。
    sections = json.loads(files("component_library").joinpath("jlc_categories.json").read_text("utf-8"))
    parents: dict[str, str] = {}
    children: dict[str, tuple[str, str]] = {}
    abbreviations: dict[str, set[tuple[str, str]]] = {}
    for parent, names in sections.items():
        parents[_category_key(parent)] = parent
        for name in names:
            key = _category_key(name)
            children.setdefault(key, (parent, name))
            shortened = re.sub(r"\([^()]+\)$", "", key)
            abbreviations.setdefault(shortened, set()).add((parent, name))
    for shortened, matches in abbreviations.items():
        if len(matches) == 1:
            children.setdefault(shortened, next(iter(matches)))
    return parents, children


def catalog_category_path(category: str | None) -> tuple[str, str]:
    label = (category or "").strip() or "未分类"
    key = _category_key(label)
    key = {"led": "发光二极管/led", "mosfet": "场效应管(mosfet)"}.get(key, key)
    parents, children = _jlc_category_index()
    if parent := parents.get(key):
        return parent, "未细分"
    return children.get(key, ("未归类", label))


def catalog_path(inventory: Path, catalog: Catalog) -> Path:
    return inventory.with_name(f"{inventory.stem}.{catalog}.db")


def initialize_catalog(connection: sqlite3.Connection, catalog: Catalog) -> None:
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {catalog}.catalog_metadata(
        kind TEXT PRIMARY KEY, version INTEGER NOT NULL)"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS {catalog}.catalog_components(
        id TEXT PRIMARY KEY, category TEXT, name TEXT NOT NULL, payload TEXT NOT NULL)"""
    )
    connection.execute(
        f"""CREATE INDEX IF NOT EXISTS {catalog}.catalog_categories
        ON catalog_components(category, name)"""
    )
    connection.execute(
        f"INSERT OR IGNORE INTO {catalog}.catalog_metadata VALUES(?,1)", (catalog,)
    )


def store_catalog_entry(
    connection: sqlite3.Connection, catalog: Catalog, component_id: str, payload: dict[str, Any]
) -> None:
    component = payload["component"]
    connection.execute(
        f"""INSERT INTO {catalog}.catalog_components(id,category,name,payload)
        VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        category=excluded.category,name=excluded.name,payload=excluded.payload""",
        (
            component_id, component.get("category"), component["name"],
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
