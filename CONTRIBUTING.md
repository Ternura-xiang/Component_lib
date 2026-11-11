# 参与开发

使用 Python 3.12 和 `uv sync --locked --extra dev` 准备桌面环境。修改安卓端时按[安卓构建说明](android/README.md)配置 JDK 和 SDK。提交前根据[测试指南](docs/developer/testing.md)运行相关回归测试。

- 保持 SQLite 为唯一库存数据源。界面和安卓客户端只展示草稿或提交操作命令，库存变动需用户确认。
- 更新功能时同步对应的[用户文档](docs/user/README.md)和接口约定。版本说明放在 `docs/releases/`，原始图标由两端共用。
- 提交前查看 `git status --short`、`git diff --cached --stat` 和 `git diff --cached --check`。保留锁文件和 Gradle Wrapper，避免提交构建产物、签名密钥、局域网证书、真实订单、库存数据库和未脱敏诊断日志。
- 新问题请附操作步骤、版本、系统和脱敏后的日志。相机问题同时注明使用的是桌面相机还是安卓相机。
