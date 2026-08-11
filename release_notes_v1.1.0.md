# FlipperVM v1.1.0 正式版

> **使用官方真实固件 Momentum v1.1.5，未使用自定义固件。**
> **Unicorn 引擎模拟 STM32WB55 Cortex-M4，FreeRTOS 调度器完整运行。**

## 下载与安装

### 方式一：下载源码包（推荐）

1. 下载下方 `flippervm-v1.1.0.zip` 或 `flippervm-v1.1.0.tar.gz`
2. 解压后进入目录
3. 安装依赖：`pip install -r requirements.txt`
4. 启动 Web UI：`python3 flippervm_webui.py`
5. 浏览器打开 `http://0.0.0.0:7860/`

### 方式二：克隆仓库

```bash
git clone https://github.com/k20120509/flippervm.git
cd flippervm
pip install -r requirements.txt
python3 flippervm_webui.py
```

## 核心特性

### 官方真实固件支持
- 加载 Momentum v1.1.5 官方固件 (867KB)
- Unicorn 引擎模拟 STM32WB55 Cortex-M4
- FreeRTOS 调度器完整运行 (SVC/SysTick/PendSV)

### LCD 屏幕正常显示
- ST7567 128x64 显示屏仿真
- FLIPPER ZERO 主菜单已显示
  - 标题: FLIPPER ZERO
  - 操作: SAVE / BACK
  - 图标: Sub-GHz, NFC, IR, GPIO 等

### Web 操作界面
- Gradio Web UI，浏览器直接访问，无需 VNC
- 6 键操作: 上/下/左/右/OK/Back
- 运行控制: 运行/暂停/单步/复位
- 速度调节: 1-500 kIPS
- UART 实时控制台输出

### FreeRTOS 稳定性修复
- PRIMASK / BASEPRI 中断屏蔽：保护临界区不被打断
- vListInsert 入口钩子：自动检测已在列表中的项，执行 uxListRemove
- 卡死循环恢复：链表自引用环检测与自动重置
- DWT_CYCCNT 动态计算：延时循环不再卡死
- SCB_VTOR 强制保护：防止 SystemInit 清零 VTOR 导致 MSP=0
- I2C1 / LPUART1 外设仿真：固件初始化不再死循环

## 验证结果

| 项目 | 状态 |
|------|------|
| LCD 主菜单显示 | 通过 |
| UART 日志输出 | 通过 |
| Web UI 访问 | 通过 |
| 按键操作 | 通过 |
| 真实固件加载 | 通过 |
| 5M 指令稳定运行 | 通过 |

## 包含文件

```
flippervm-v1.1.0/
├── flipper_vm/                    # 核心仿真引擎
│   ├── emulator.py                # 仿真核心 (80KB)
│   ├── firmware_loader.py         # 固件加载器
│   ├── stm32wb55.py               # STM32WB55 外设定义
│   ├── display.py                 # LCD 显示适配器
│   ├── gui.py                     # 桌面 GUI
│   ├── _version.py                # 版本号 v1.1.0
│   └── __init__.py
├── firmware_files/                # 固件文件
│   ├── momentum-fw-v1.1.5.dfu     # 官方真实固件 (867KB)
│   ├── demo_firmware.bin          # 演示固件
│   └── OTP.bin
├── flippervm_webui.py             # Web UI 入口
├── main.py                        # 主程序入口
├── requirements.txt               # Python 依赖
├── README.md                      # 项目文档
├── LICENSE                        # MIT 许可证
├── docs/                          # 文档与截图
├── boot_lcd_final.png             # 启动画面截图
└── tests/                         # 测试脚本
    ├── test_integration.py
    ├── test_real_firmware.py
    ├── test_smoke.py
    └── test_systick.py
```

## 系统要求

- Python 3.10+
- Unicorn Engine
- Gradio 4.x / 5.x / 6.x
- Pillow
- capstone (可选，用于反汇编)

## 技术支持

- 仓库: https://github.com/k20120509/flippervm
- Issues: https://github.com/k20120509/flippervm/issues

---

*本版本使用官方真实固件 (Momentum v1.1.5)，未使用自定义固件。*
