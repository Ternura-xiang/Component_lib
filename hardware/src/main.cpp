/**
 * @file main.cpp
 * @brief ESP32-C3 固件入口：启动 USB 日志和各业务模块。
 * @details LED 与触摸在各自的 FreeRTOS 任务中运行；Wi-Fi 连接由框架后台处理。
 *          屏幕初始化当前停用，保留原调用位置供后续屏幕调试使用。
 */
#include <Arduino.h>
#include "usb_serial.h"

#include "wifi.h"
#include "LED.h"
// #include "disp.h"
#include "touch.h"

// USB1 的硬件 CDC 对象仅在此定义一次，其他模块通过 usb_serial.h 的 extern 使用。
// 显式使用 HWCDC，避免依赖 .ini 中的 USB 模式宏；UART0 可留给扫码头。
HWCDC USBSerial;

/**
 * @brief Arduino 上电或复位后执行一次的初始化入口。
 * @note 先启动 USB 日志，再初始化模块，方便输出模块初始化或任务创建失败信息。
 *       此处不等待电脑打开串口，也不等待 Wi-Fi 连接成功，避免阻塞触摸和状态灯启动。
 */
void setup()
{
	USBSerial.begin(115200);
	USBSerial.println("System Init...");

	// 初始化顺序：状态灯 → Wi-Fi 异步连接 → 触摸采样任务。
	LED_Init();
	wifi_init();
	// DISP_Init();
	Touch_Init();

	USBSerial.println("System initialized");
}

/**
 * @brief Arduino 主循环，当前业务由模块任务执行。
 * @note 当前未在此消费 touch_event；若要绑定扫码、背光等动作，可在此处理最新事件。
 */
void loop()
{

}
