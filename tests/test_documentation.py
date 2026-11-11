"""说明文档的轻量一致性测试。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def documentation_files() -> list[Path]:
    """返回需要保持链接有效的项目文档。"""

    return [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "android" / "README.md",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "tools").rglob("README.md"),
    ]


def test_readme_version_matches_project_metadata() -> None:
    """README 展示版本必须与 pyproject 元数据一致。"""

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"当前版本为 **{version}**" in readme
    release_notes = ROOT / "docs" / "releases" / f"v{version}.md"
    assert release_notes.is_file()
    assert release_notes.read_text(encoding="utf-8").startswith(f"# 元件库 v{version} 发行说明\n")


def test_project_declares_mit_license() -> None:
    """打包元数据、README 和许可证文件必须对开源协议表述一致。"""

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Component Library contributors" in license_text
    assert "[MIT License](LICENSE)" in readme


def test_relative_documentation_links_exist() -> None:
    """文档中的相对文件链接不得指向已删除路径。"""

    missing: list[str] = []
    for source in documentation_files():
        text = source.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (source.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{source.relative_to(ROOT)} -> {raw_target}")
    assert not missing, "\n".join(missing)
