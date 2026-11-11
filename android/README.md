# 元件库 Android 移动端

当前版本为 **1.0.9**，支持 Android 8.0（API 26）及以上。移动端是桌面应用的局域网摄像头和 BOM 取货终端。库存、OCR 和取货校验由桌面端处理。

## 构建

- JDK 17
- Android SDK Platform 37 和 Build Tools 37.0.0
- 仓库自带 Gradle Wrapper 9.6。通过 Android Studio 或 `ANDROID_HOME` 指定 SDK，亦可使用本机 `local.properties`。

```bash
./gradlew :app:assembleDebug :app:testDebugUnitTest
```

在 `android/` 目录运行以上命令。Windows 使用 `gradlew.bat`。调试 APK 位于 `app/build/outputs/apk/debug/app-debug.apk`，可侧载或用 `adb install -r app/build/outputs/apk/debug/app-debug.apk` 安装。首次构建需要下载锁定的工具链与依赖。

## 配对与操作

- 电脑和手机接入同一局域网，电脑进入 **设置 → 连接信息** 生成配对二维码。在手机扫码完成 HTTPS 配对。手机用 Android Keystore 保护设备令牌，并通过 mDNS 发现电脑。发现失败时使用配对记录中的地址。
- 入库拍摄标签后由电脑 OCR，两端同步草稿。出库持续识别批次码，数量和确认按钮位于取景画面下方，仓库、余量和备注在同一页面。取景区域按相机输出比例调整，小屏或横屏时可滚动。
- BOM 取货任务由电脑创建，手机可以扫码、打钩、撤销和提交。库存写入需要桌面在线，断网不会排队补交库存操作。
- 退出电脑当前入库或出库页面会关闭对应会话。电脑也可在设置中删除已配对设备。具体流程见[扫码与移动摄像头](../docs/user/scanning.md)。

## 诊断

扫码无响应时先检查相机权限，并可点击二维码所在位置对焦。复现后可回到首页使用「复制诊断日志」。日志保存在应用私有目录并限制大小，记录权限、相机绑定、帧信息及识别阶段。分享前请检查并删除可能暴露设备或内网的信息。

`ANALYZER_FIRST_FRAME`、`MLKIT_CODES` 和 `BARCODE_DELIVERED` 用于定位扫码阶段。`PHOTO_CAPTURED`、`IMAGE_UPLOAD_START` 和 `IMAGE_UPLOAD_OCR_COMPLETE` 用于定位拍照、传输及桌面识别耗时。协议和版本协商见 [API 文档](../docs/developer/api.md)。
