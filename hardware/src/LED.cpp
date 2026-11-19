/********************************************************************************
* @author: ternura-xiang
* @email: ternura_xiang@foxmail.com
* @date: 2026-11-19 03:37
* @version: 1.0.0
* @description: 六颗 WS2812B 状态灯初始化与周期闪烁任务
* @section 更新历史
* | 版本号 | 修改说明 | 修订者 | 修订日期 |
* |----------|----------------|----------|------------|
* | v1.0.0 | 创建文档 | ternura-xiang | 2026-11-19 |
********************************************************************************/
#include <Arduino.h>
#include "usb_serial.h"
#include "LED.h"
#include <FastLED.h>

/*******************************************************************************
 * Definitions
 ******************************************************************************/
// WS2812B 共用一根数据线；灯珠按串接顺序对应 leds 数组下标。
#define LED_PIN		1
#define LED_NUM		6
#define LED_TYPE	WS2812B
#define COLOR_ORDER GRB	// 灯珠接收的分量顺序，和 CRGB 的逻辑 RGB 表示相区分。

// 颜色缓冲区：fill_solid 只修改内存，调用 FastLED.show() 才会发送到灯珠。
CRGB leds[LED_NUM];
// 保存创建出的任务句柄；当前实现不对任务执行挂起或删除操作。
TaskHandle_t LED_TaskHandle = nullptr;
/*******************************************************************************
 * Code
 ******************************************************************************/
/**
 * @brief 周期翻转六颗灯珠，提供固件运行中的可视状态指示。
 * @param pvParameters FreeRTOS 任务入口参数，当前未使用。
 * @details 每隔约 1000 ms 在 Orange3 与熄灭之间切换，完整明暗周期约为 2 s。
 *          vTaskDelay 使用 tick，先从毫秒转换；等待期间让出 CPU 给其他任务。
 */
static void LED_Task(void *pvParameters)
{
	constexpr TickType_t delay = pdMS_TO_TICKS(1000);
	static bool led_on = false;

	for (;;)
	{
		// 同步更新所有灯珠，再一次性发送整条灯带的数据。
		led_on = !led_on;
		fill_solid(leds, LED_NUM, led_on ? CRGB::Orange3 : CRGB::Black);
		FastLED.show();
		vTaskDelay(delay);
	}
}

/**
 * @brief 注册灯带、设置初始亮度，并启动周期闪烁任务。
 * @note 应在 USBSerial.begin() 后调用，以便输出任务创建结果；按当前入口调用一次。
 *       此函数未做重复初始化保护，不能在 loop() 中反复调用。
 */
void LED_Init()
{
	// 指定实际芯片型号、数据 GPIO 和颜色顺序，注册颜色缓冲区。
	FastLED.addLeds<LED_TYPE, LED_PIN, COLOR_ORDER>(leds, LED_NUM);
	FastLED.setBrightness(10);		// 全局亮度为 10/255，降低上电测试时的功耗与眩光。

	// 创建任务前先发送全黑，避免沿用上次供电状态或缓冲区内容。
	fill_solid(leds, LED_NUM, CRGB::Black);
	FastLED.show();

	// ESP32 的任务栈参数按字节计；栈大小 2048 字节，任务优先级为 10。
	if (xTaskCreate(LED_Task, "LED_Task", 2048, nullptr,
		10, &LED_TaskHandle) != pdPASS)
	{
		USBSerial.println("LED_Task creation failed");
	}

	// 此日志表示初始化流程走完；任务是否创建成功需结合上方失败日志判断。
	USBSerial.println("LED Init Success");
}
