# 桌面 UI、任务执行与打包

## 页面构成

`ui/main_window.py` 的 `MainWindow` 组织侧边栏主页面：**元件库存、入库、出库、位置与仓库、扫描入库、库存流水、OCR 样本、设置与备份**。

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

## 运行环境

Python 要求 `>=3.12,<3.13`。Linux 需要 Qt Multimedia/GStreamer、Qt WebEngine 相关系统库。摄像头和网页浏览器是可降级功能，但其依赖缺失不能被误判为库存模块本身不可用。

## 打包

项目提供 `packaging/component-library.spec`、`packaging/entry.py`、Windows Inno Setup 脚本及 Linux 桌面入口。PyInstaller 构建命令：

```bash
uv sync --extra dev
uv run python tools/build.py
```

产物默认位于 `dist/component-library/`。构建目标是**当前宿主平台**的 onedir 产物，不能假定在 Linux 一次构建就得到 Windows 安装程序。发布前检查 OCR 模型、Qt Multimedia/WebEngine、`component_library/assets` 静态资源、启动与退出流程，并在目标系统实际执行一次扫码/导入/备份冒烟测试。
