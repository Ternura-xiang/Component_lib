/********************************************************************************
* @author: ternura-xiang
* @email: ternura_xiang@foxmail.com
* @date: 2026-11-19 06:34
* @version: 1.0.0
* @description: 两路低电平有效触摸输入的点击、双击、长按和滑动识别
* @section 更新历史
* | 版本号 | 修改说明 | 修订者 | 修订日期 |
* |----------|----------------|----------|------------|
* | v1.0.0 | 创建文档 | ternura-xiang | 2026-11-19 |
********************************************************************************/
#include "touch.h"
#include <Arduino.h>
#include "usb_serial.h"

/*******************************************************************************
 * Definitions
 ******************************************************************************/
// 两路 JSM8223 数字输出；OLH 接 3V3，因此低电平才表示触摸。
#define TOUCH_PIN_1		3
#define TOUCH_PIN_2		4

// 时间参数单位均为毫秒；长按窗口大于滑动窗口，避免滑动尚未完成就报告长按。
#define LONG_PRESS_THRESHOLD	600
#define DOUBLE_CLICK_THRESHOLD	300
#define SWIPE_THRESHOLD			400
#define TOUCH_COMBINE_THRESHOLD	30	// 共用于启动松手、手势结束和目标单区确认。

// 保存最新事件，初始无事件。volatile 确保每次访问内存，不提供队列或读改写互斥。
// 消费方处理后可置 NONE；若消费不及时，新事件会覆盖旧事件。
volatile TOUCH_EVENT_t touch_event = TOUCH_EVENT_NONE;
/*******************************************************************************
 * Code
 ******************************************************************************/
/**
 * @brief 持续采样两路触摸输入，按一次完整按压过程识别手势。
 * @param pvParameters FreeRTOS 任务参数，当前未使用。
 * @details 启动等待松手 → 检测按下 → 跟踪两区变化 → 稳定松手 → 发布手势。
 *          长按在保持期间发布一次；滑动和双击在松手后发布；单击需等双击窗口结束。
 *          所有等待均调用 vTaskDelay，让出 CPU，不使用中断或另建事件队列。
 */
static void Touch_Task(void *pvParameters)
{
	// 在读取边界统一转为“true = 被触摸”，后续逻辑不直接依赖原始 GPIO 极性。
	bool touch1_value = digitalRead(TOUCH_PIN_1) == LOW;
	bool touch2_value = digitalRead(TOUCH_PIN_2) == LOW;
	// 两区共享待确认单击，因此第二次点击可使用任意一个区域或两个区域。
	bool click_pending = false;
	unsigned long last_click_time = 0;
	// 常规采样间隔为 5 ms；若系统 tick 较粗，至少等待一个 tick，避免忙等。
	const TickType_t delay = pdMS_TO_TICKS(5) > 0 ? pdMS_TO_TICKS(5) : 1;

	// 启动时已有的按住电平不算新手势。两路连续松开 30 ms 后才进入识别；
	// 等待中任一路再次触摸都会重置计时，避免复位时直接误发长按。
	unsigned long idle_time = millis();
	while (millis() - idle_time < TOUCH_COMBINE_THRESHOLD)
	{
		if (digitalRead(TOUCH_PIN_1) == LOW || digitalRead(TOUCH_PIN_2) == LOW)
			idle_time = millis();
		vTaskDelay(delay);
	}

	while (1)
	{
		// 单击等待双击窗口结束后再确认，两个区域共用点击状态。
		if (click_pending && millis() - last_click_time > DOUBLE_CLICK_THRESHOLD)
		{
			touch_event = TOUCH_EVENT_CLICK;
			USBSerial.println("Click");
			click_pending = false;
		}

		touch1_value = digitalRead(TOUCH_PIN_1) == LOW;
		touch2_value = digitalRead(TOUCH_PIN_2) == LOW;
		if (!touch1_value && !touch2_value)
		{
			vTaskDelay(delay);
			continue;
		}

		// 记录本次按下时刻，等待一个采样周期后再确认，过滤短于采样周期的毛刺。
		unsigned long time = millis();
		vTaskDelay(delay);
		touch1_value = digitalRead(TOUCH_PIN_1) == LOW;
		touch2_value = digitalRead(TOUCH_PIN_2) == LOW;
		if (!touch1_value && !touch2_value) continue;

		// 1/2 表示单区，3 表示双区；保留最初区域，避免快速滑动丢失方向。
		int first_touch = (touch1_value ? 1 : 0) | (touch2_value ? 2 : 0);
		// 最近一次单区状态及进入该单区的时刻，双区重叠不会清除已知方向。
		int last_touch = first_touch;
		unsigned long last_touch_time = time;
		// 在第二次按下时判断间隔；第二次按压最终若成为滑动或长按，则不发双击。
		bool double_click = click_pending && time - last_click_time <= DOUBLE_CLICK_THRESHOLD;
		bool event_sent = false;	// 长按已经发布，松手后不再追加其他事件。
		bool swipe = false;		// 已满足滑动条件后保持为 true，避免松手回弹撤销结果。
		unsigned long released_time = 0;	// 首次检测到两区全松开的时刻。
		bool releasing = false;	// 区分“正在确认松手”与“仍在触摸”，不把时刻 0 当哨兵。

		while (1)
		{
			touch1_value = digitalRead(TOUCH_PIN_1) == LOW;
			touch2_value = digitalRead(TOUCH_PIN_2) == LOW;
			// 位 0 对应 TOUCH1，位 1 对应 TOUCH2；0/1/2/3 表示空闲/单区1/单区2/双区。
			int current_touch = (touch1_value ? 1 : 0) | (touch2_value ? 2 : 0);
			unsigned long now = millis();
			if (current_touch == 0)
			{
				// 只在进入全松开状态时计时；后续空闲样本不能不断重置起点。
				if (!releasing)
				{
					released_time = now;
					releasing = true;
				}
				// 容许电极间短暂空隙，也过滤松手抖动。
				if (now - released_time >= TOUCH_COMBINE_THRESHOLD) break;
			}
			else
			{
				// 松手确认期间再次触摸，继续当前手势，允许跨越电极间短暂空隙。
				releasing = false;
				if (current_touch == 1 || current_touch == 2)
				{
					if (last_touch != current_touch) last_touch_time = now;
					last_touch = current_touch;
				}
				// 起始双区不能提供方向；单区起始则要求在 400 ms 内到达另一单区，
				// 且距最近单区切换至少 30 ms（中间双区重叠不重置此计时）。
				// 仅“单区 → 双区”不算滑动；满足条件后锁定结果，松手抖动不再撤销。
				swipe = swipe || (first_touch != 3 && last_touch != first_touch
					&& last_touch_time - time <= SWIPE_THRESHOLD
					&& now - last_touch_time >= TOUCH_COMBINE_THRESHOLD);
				// 尚未确认滑动且持续触摸达到长按阈值时仅发布一次，之后等待全部松手。
				if (!event_sent && !swipe && now - time >= LONG_PRESS_THRESHOLD)
				{
					touch_event = TOUCH_EVENT_LONG_PRESS;
					USBSerial.println("Long Press");
					event_sent = true;
				}
			}
			vTaskDelay(delay);
		}

		// 一次手势只走一个分支：已发长按 → 已确认滑动 → 双击 → 待确认单击。
		if (event_sent)
		{
			click_pending = false;
		}
		else if (swipe)
		{
			// 当前实机方向约定：TOUCH1 → TOUCH2 为上划，反向为下划。
			touch_event = first_touch == 1 ? TOUCH_EVENT_UP : TOUCH_EVENT_DOWN;
			USBSerial.println(first_touch == 1 ? "TOUCH UP" : "TOUCH DOWN");
			click_pending = false;
		}
		else if (double_click)
		{
			touch_event = TOUCH_EVENT_DOUBLE_CLICK;
			USBSerial.println("Double Click");
			click_pending = false;
		}
		else
		{
			// 若旧单击仍待确认但本次不构成双击，先发布旧单击，再等待本次的双击窗口。
			if (click_pending)
			{
				touch_event = TOUCH_EVENT_CLICK;
				USBSerial.println("Click");
			}
			click_pending = true;
			// 双击间隔从首次全松开时计，不额外包含后面的松手确认等待。
			last_click_time = released_time;
		}
	}
}

/**
 * @brief 配置两路数字输入并创建触摸采样任务。
 * @note 由 setup() 调用一次，依赖 USBSerial 已初始化以便报告任务创建失败。
 *       当前没有重复调用保护，不应在主循环内反复创建采样任务。
 */
void Touch_Init()
{
	// 读取触摸芯片的数字输出，不使用 ESP32 的电容触摸外设，也不启用内部下拉。
	pinMode(TOUCH_PIN_1, INPUT);
	pinMode(TOUCH_PIN_2, INPUT);

	// ESP32 任务栈参数单位为字节：2048 字节栈、优先级 5，无需保留任务句柄。
	if (xTaskCreate(Touch_Task, "TOUCH_Task", 2048, NULL, 5, NULL) != pdPASS)
	{
		USBSerial.println("TOUCH_Task creation failed");
	}
}
