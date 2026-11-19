#ifndef HARDWARE_USB_SERIAL_H
#define HARDWARE_USB_SERIAL_H

/**
 * @file usb_serial.h
 * @brief 模块间共享 USB1 的硬件 CDC 日志对象声明。
 * @details 此对象在 main.cpp 中定义并初始化，不能在头文件中重复创建实例。
 */
#include <HWCDC.h>

// USB1 使用 ESP32-C3 硬件 USB Serial/JTAG，UART0 留给扫码头。
// 各模块直接使用 USBSerial，避免通过全局编译宏改变 Arduino Serial 的含义。
// setup() 已执行 begin(115200) 后，其他模块才能使用它输出日志。
extern HWCDC USBSerial;

#endif // HARDWARE_USB_SERIAL_H
