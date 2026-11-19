/********************************************************************************
* @author: ternura-xiang
* @email: ternura_xiang@foxmail.com
* @date: 2026-11-19 03:26
* @version: 1.0.0
* @description: ST7789 屏幕初始化预留代码，当前显示操作均已停用
* @section 更新历史
* | 版本号 | 修改说明 | 修订者 | 修订日期 |
* |----------|----------------|----------|------------|
* | v1.0.0 | 创建文档 | ternura-xiang | 2026-11-19 |
********************************************************************************/
#include "disp.h"
// #include <TFT_eSPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

/*******************************************************************************
 * Definitions
 ******************************************************************************/
// TFT_eSPI tft = TFT_eSPI();

// ST7789 屏幕信号与原理图 GPIO 对应；当前仅保留定义，不执行显示初始化。
#define TFT_SCK     6
#define TFT_MOSI    7
#define TFT_RST     8
#define TFT_DC      5
// CS 默认由板上电阻拉低；若改为 GPIO10 控制，需同时核对实际焊接配置。
// #define TFT_CS      10
// 以下背光定义是停用的历史调试代码；原理图 LCD_LED 使用 GPIO2。
// #define TFT_BL      16

/*******************************************************************************
 * Code
 ******************************************************************************/
/**
 * @brief 屏幕初始化接口占位，保留两种库的历史调试步骤。
 * @return 当前固定返回 true，仅表示执行到函数末尾，不代表屏幕已初始化或点亮。
 * @note 所有显示与背光操作均被注释，main.cpp 中的调用也停用。
 *       重新启用前应先确认使用的显示库、屏幕尺寸、片选和背光 GPIO。
 */
bool DISP_Init(void)
{
	// GPIO2 控制背光，先关闭，避免看到 LCD 初始化时的显存内容。
	// pinMode(TFT_BL, OUTPUT);
	// digitalWrite(TFT_BL, LOW);

	// tft.init();
	// tft.setRotation(0);
	// tft.invertDisplay(false);
	// tft.fillScreen(TFT_WHITE);

	// Adafruit 方案的历史测试代码：实际 init 参数为 240×320。
	// 不能仅依据旧注释中的屏幕尺寸判断分辨率，重新启用时应核对实物。
	// Adafruit_ST7789 tft(TFT_CS, TFT_DC, TFT_RST);
	// tft.init(240, 320);
	// tft.setRotation(2);//转180度
	// tft.fillScreen(ST77XX_WHITE);


	// digitalWrite(TFT_BL, HIGH);
	// 当前无硬件初始化动作，也没有采集屏幕状态来判断成功与否。
	return true;
}
