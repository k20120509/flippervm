"""构建演示固件 demo_firmware.bin,用于让用户验证 FlipperVM 是否正常工作。

该固件模拟 Flipper Zero 的最小启动流程:
  1. Reset_Handler 设置 VTOR 和 SP
  2. furi_hal_init_early 等待 RCC/FLASH/PWR/RNG/HSEM 就绪
  3. furi_run 启动 FreeRTOS(SVC + SysTick)
  4. InitSrv -> HAL full + BT + GUI 初始化
  5. GUI 在 ST7567 LCD 上绘制测试图案
  6. UART 输出 FIRMWARE_BOOTED dolphin

运行后会输出 demo_firmware.bin,用户可以在 FlipperVM 中加载该文件。
"""
import struct
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_boot_flow import build_firmware


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "firmware_files", "demo_firmware.bin")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    fw = build_firmware()
    with open(out_path, "wb") as f:
        f.write(fw.data)

    size = os.path.getsize(out_path)
    print(f"[OK] 演示固件已生成: {out_path}")
    print(f"     大小: {size} bytes ({size // 1024} KB)")
    print(f"     base_addr  = 0x{fw.base_addr:08X}")
    print(f"     entry      = 0x{fw.entry_point:08X}")
    print(f"     initial_sp = 0x{fw.initial_sp:08X}")
    print()
    print("使用方法:")
    print("  1. 启动 FlipperVM.exe")
    print("  2. 点「加载固件」按钮")
    print(f"  3. 选择 {out_path}")
    print("  4. 点「▶ 运行」")
    print("  5. LCD 屏幕应显示测试图案,UART 控制台输出 FIRMWARE_BOOTED dolphin")


if __name__ == "__main__":
    main()
