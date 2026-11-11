# 桌面 UI、任务执行与打包

## 页面构成

`ui/main_window.py` 的 `MainWindow` 组织侧边栏主页面：**元件库存、入库、出库、位置与仓库、库存流水、设置**。扫描/图片识别作为入库子页面，BOM 作为出库子页面。

订单导入是**入库的子页面**，BOM 是**出库的子页面**；它们不是侧边栏的独立主入口。修改导航时同时检查子页返回逻辑和刷新行为。

相关文件：

- `ui/main_window.py`：页面、表单、业务操作反馈。
- `ui/jlc_browser.py`：临时商品页浏览器及确认流程。
- `ui/workers.py`：耗时任务在线程池中的执行与回调。
- `assets/checkmark.svg`：普通复选框、表格复选框和可折叠分组框共用的黑色对号。

## UI 扩展约定

- 不能阻塞 Qt 主线程运行 OCR、批量解析或网络预览。
- 使用原有后台任务机制执行耗时计算，并在 Qt 主线程更新控件。
- 写数据只走 `Database` 公共方法，成功后刷新相关页面。
- 所有来源数据要先可视化预览，用户确认后才提交；对可能丢失数据的操作提供错误提示。
- 扫描页面负责停止与释放摄像头，避免关闭窗口后设备仍被占用。
- 复选框视觉由全局 Qt Style Sheet 统一；修改资源路径后必须同时验证源码运行和 PyInstaller 产物。
- 设置页的调试模式使用 `QSettings` 保存。源码运行首次默认开启，PyInstaller 打包版本首次默认关闭；帧计数等诊断不得在关闭调试时混入正常识别状态。

## 运行环境

Python 要求 `>=3.12,<3.13`。Linux 需要 Qt Multimedia/GStreamer、Qt WebEngine 相关系统库。摄像头和网页浏览器是可降级功能，但其依赖缺失不能被误判为库存模块本身不可用。

## 打包

项目提供 `packaging/component-library.spec`、`packaging/entry.py`、Windows Inno Setup 脚本及 Linux 桌面入口。PyInstaller 构建命令：

```bash
uv sync --locked --extra dev
uv run python tools/build.py
```

产物默认位于 `dist/component-library/`。构建目标是**当前宿主平台**的 onedir 产物，不能假定在 Linux 一次构建就得到 Windows 安装程序。发布前检查 OCR 模型、Qt Multimedia/WebEngine、`component_library/assets` 静态资源、启动与退出流程，并在目标系统实际执行一次扫码/导入/备份冒烟测试。

Windows 构建完成后运行 `uv run python tools/smoke_windows.py`，检查真实无控制台 exe 的启动、认证 API 和离线模型校验。脚本使用临时 APPDATA 与 offscreen Qt 平台，结束后回收进程；运行前须关闭占用 17635 端口的实例。此测试不覆盖摄像头及 WebEngine 交互。

桌面和安卓共享 `packaging/assets/icons/component-library.png`。Windows 构建会生成安装器使用的 ICO，安卓构建会把同一 PNG 放入 APK 资源。参见[安卓构建说明](../../android/README.md)。

桌面 API 使用应用统一配置的文件日志（Uvicorn 的 `log_config=None`）。不要恢复默认控制台日志配置：Windows `console=False` 产物的 `sys.stdout` / `sys.stderr` 为 `None`，默认 formatter 会使程序启动失败。

Windows spec 排除 `icuuc.dll`，由 Windows 10/11 系统提供 Qt 所需 ICU。构建环境 PATH 中 Poppler 等工具的同名 DLL 可能使用不同符号版本，误收集会导致 `QtCore` 报 DLL 找不到指定程序。
