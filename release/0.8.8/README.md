# Component Library 0.8.8 发行文件

## 可运行程序

- component-library-0.8.8-linux-x86_64.tar.gz：Linux x86-64 PyInstaller onedir 程序。已执行无显示启动、本机 API 和离线 OCR 模型校验。
- win-0.8.8.zip：已有的 Windows x86-64 onedir 程序包。本次在 Linux 上只检查 ZIP 完整性和必要文件；正式发布前仍需在 Windows 10/11 上运行 tools/smoke_windows.py，并手工验证摄像头与 WebEngine。

Linux 解压后运行：

    tar -xzf component-library-0.8.8-linux-x86_64.tar.gz
    cd component-library-0.8.8-linux-x86_64
    ./component-library

Windows 应完整解压 ZIP，保留 component-library.exe 与 _internal 的相对位置，然后运行 component-library.exe。

## Python 发布包

- component_library-0.8.8-py3-none-any.whl：Python wheel。
- component_library-0.8.8.tar.gz：标准源码发布包。
- linux-0.8.8.tar.gz：早期保留的源码快照；尽管名称中包含 linux，它不是可直接运行的 Linux 程序包。

## 其他文件

- RELEASE_NOTES.md：功能、修复、升级和已知限制。
- LICENSE：MIT License。
- SHA256SUMS：发行附件的 SHA-256 校验值。

校验下载文件：

    sha256sum --check SHA256SUMS
