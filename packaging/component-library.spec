# PyInstaller 配置本身由 Python 执行，因此保留注释说明离线模型的收集原因。
from PyInstaller.utils.hooks import collect_all, collect_data_files

rapidocr_datas, rapidocr_binaries, rapidocr_hidden = collect_all("rapidocr")
app_datas = collect_data_files("component_library")

a = Analysis(
    ["entry.py"],
    pathex=["../src"],
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
