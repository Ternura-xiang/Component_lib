/********************************************************************************
* @author: ternura-xiang
* @email: ternura_xiang@foxmail.com
* @date: 2026-11-19 06:34
* @version: 1.0.0
* @description: 两路触摸手势事件定义、最新事件标志与初始化接口
* @section 更新历史
* | 版本号 | 修改说明 | 修订者 | 修订日期 |
* |----------|----------------|----------|------------|
* | v1.0.0 | 创建文档 | ternura-xiang | 2026-11-19 |
********************************************************************************/
#ifndef HARDWARE_TOUCH_H
#define HARDWARE_TOUCH_H

/**
 *  @addtogroup touch.h
 *  @{
 */

/*******************************************************************************
 * Definitions
 ******************************************************************************/
/**
 * @brief 两区共用的手势类型，不区分触发点击、双击或长按的是哪一触摸区域。
 * @details 滑动方向约定：TOUCH1 → TOUCH2 为 UP，反向为 DOWN。
 *          单击需等双击窗口结束；双击和滑动在松手后确认；长按在保持期间确认。
 */
typedef enum
{
	TOUCH_EVENT_NONE = 0,		// 无事件
	TOUCH_EVENT_CLICK,			// 单击
	TOUCH_EVENT_DOUBLE_CLICK,	// 双击
	TOUCH_EVENT_LONG_PRESS,		// 长按
	TOUCH_EVENT_UP,				// 上划
	TOUCH_EVENT_DOWN,			// 下划
}TOUCH_EVENT_t;

/*******************************************************************************
 * API
 ******************************************************************************/
#ifdef  __cplusplus
extern "C" {
#endif // __cplusplus

/**
 * @brief 触摸任务发布的最新事件，初始为 TOUCH_EVENT_NONE。
 * @note 消费方处理后可写回 NONE；此标志不缓存事件，也不提供消费确认或线程互斥。
 *       volatile 不保证“读取并清空”作为一个原子操作，不能将其当成事件队列使用。
 */
extern volatile TOUCH_EVENT_t touch_event;

/**
 * @brief 配置 GPIO3/4 输入并创建采样任务，两路均为低电平触摸有效。
 * @note 上电先等待两区稳定松开；在 setup() 中、USBSerial 初始化后调用一次。
 *       当前任务栈为 2048 字节，创建失败仅打印日志，不返回状态。
 */
void Touch_Init(void);

#ifdef  __cplusplus
}
#endif // __cplusplus

/**
 *  @}
 */
 
#endif //HARDWARE_TOUCH_H
