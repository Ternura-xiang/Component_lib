# PyInstaller 配置本身由 Python 执行，因此保留注释说明离线模型的收集原因。
from pathlib import Path

from PyInstaller.compat import is_win
from PyInstaller.utils.hooks import collect_all, collect_data_files

root = Path(SPECPATH).parent

rapidocr_datas, rapidocr_binaries, rapidocr_hidden = collect_all("rapidocr")
app_datas = collect_data_files("component_library")

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=rapidocr_binaries,
    datas=rapidocr_datas + app_datas,
    hiddenimports=rapidocr_hidden
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    noarchive=False,
)
# Windows Qt 使用系统 ICU 的无版本符号。构建机 PATH 上的 Poppler 等工具
# 可能携带同名、带版本符号的 icuuc.dll；收集它会导致 QtCore 无法加载。
# Windows 10/11 自带此系统 DLL，让加载器从系统目录解析即可。
if is_win:
    a.binaries = [item for item in a.binaries if Path(item[0]).name.lower() != "icuuc.dll"]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="component-library",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="component-library")

