# FlipperVM

**Flipper Zero 主板级虚拟机** —— 基于 Unicorn Engine 的 STM32WB55 Cortex-M4 指令级仿真器。

把 Flipper Zero 固件(`.bin` / `.dfu`)丢进去,即可在 Windows 上运行并操作。

## 功能

- ARM Cortex-M4(STM32WB55)Thumb-2 指令级仿真
- Flash / SRAM / 系统内存完整映射
- 外设:GPIO(按键)、SPI2(ST7567 显示屏)、USART1/2、SysTick、NVIC、EXTI、RCC 等
- 128×64 单色 OLED 实时渲染
- 方向键 / OK / Back 完整按键映射(鼠标点 + 键盘)
- DFU 与 bin 固件加载
- UART 控制台实时输出

## 在 Windows 上运行(已打包 exe)

1. 从 [Releases](../../releases) 下载 `FlipperVM.exe`
2. 双击运行
3. 点「加载固件」选择 `.bin` 或 `.dfu`
4. 点「▶ 运行」

## 从源码运行

```bat
pip install -r requirements.txt
python main.py
```

## 自己构建 exe

```bat
build.bat
```

生成在 `dist\FlipperVM.exe`。

## 键盘映射

| 键 | Flipper 按钮 |
|---|---|
| ← → ↑ ↓ | 左 右 上 下 |
| Enter | OK |
| Esc / Backspace | Back |

## 目录结构

```
flipper_vm/
  stm32wb55.py        STM32WB55 地址映射
  display.py          ST7567 显示仿真
  firmware_loader.py  DFU/bin 加载器
  emulator.py         Unicorn 仿真核心 + 外设
  gui.py              PySide6 GUI
main.py               入口
build.bat             Windows 构建脚本
flippervm.spec        PyInstaller 配置
```

## 许可

MIT
