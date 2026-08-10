# 🐬 FlipperVM — Flipper Zero 虚拟机用户手册


**发现 bug、想加外设、提建议?** 欢迎来 https://github.com/k20120509/flippervm/issues 开 Issue。



> **一句话理解**:把 Flipper Zero 的固件放进本软件,你就能在 Windows 电脑上**像用真机一样用 Flipper Zero**,不用花钱买硬件也能体验它的界面和功能。
>
> 本软件是**主板级虚拟机**:不只是 UI 模仿,而是用 [Unicorn Engine] 真实执行 STM32WB55 的每一条 ARM 指令,加载任何官方/第三方固件,屏幕显示和按键逻辑都与真机完全一致。

```
 ╭─────────────────────────────────╮
 │  真实世界:  固件 → Flipper Zero 硬件 → 屏幕/按键/红外/RFID
 │  本软件:    固件 → FlipperVM         → 电脑屏幕/键盘鼠标
 ╰─────────────────────────────────╯
```

---

## 📦 怎么下载和启动(小白也能 30 秒搞定)

### 方式 A:便携版 — 免装、免配置,推荐 ⭐⭐⭐⭐⭐

**一次下载,永远不用配置环境。**

1. 打开 [Release 下载页](https://github.com/k20120509/flippervm/releases/latest)
2. 找到 **最新版本**(v0.2.0 或更高),下载名为
   `FlipperVM-vX.X.X-portable-win64.zip`
   的压缩包(约 260 MB)
3. **右键 → 解压到当前文件夹 / 解压到 FlipperVM 文件夹**
4. 打开解压出来的文件夹,你会看到这些文件:

   ```
   FlipperVM/
   ├── FlipperVM.bat            ← 🖱 双击这个(有控制台,可看调试信息)
   ├── FlipperVM_无控制台.bat   ← 或者双击这个(纯 GUI,无黑色窗口)
   ├── README_FIRST.txt         ← 首次使用必读
   ├── python/                  ← 自带 Python,不用你装
   └── app/                     ← 程序代码
   ```

5. 双击 **FlipperVM.bat**,软件就启动了!

> 💡 **解压注意**(非常重要):
> - 解压路径**不要包含中文或空格**,建议直接解压到 `D:\FlipperVM\` 或 `C:\Users\你的用户名\FlipperVM\`
> - ❌ 错误示例:`D:\软件\虚拟机(测试)\FlipperVM\`
> - ✅ 正确示例:`D:\Tools\FlipperVM\`

### 方式 B:从源码运行 — 适合开发者

已经装了 Python 3.9–3.12?那只要三行命令:

```bat
pip install -r requirements.txt
python main.py
```

### 方式 C:自己打包成单文件 exe

想要一个纯粹的 `FlipperVM.exe`?双击 `build.bat`,等它自动装依赖 + 打包,完成后 exe 在 `dist\FlipperVM.exe`。

---

## 🎯 启动后第一件事:加载固件并运行

### 第 1 步:准备好 Flipper 固件

FlipperVM 需要一个「Flipper Zero 的操作系统固件」才能工作。就像一台没装系统的电脑,你得先装 Windows 才能用。

固件来源任选其一:

| 类型 | 哪里下载 | 推荐 |
|---|---|---|
| **官方稳定版** | https://github.com/flipperdevices/flipperzero-firmware/releases | ✅ 适合刚上手 |
| **Momentum**(第三方) | https://github.com/Next-Flip/Momentum-Firmware/releases | 功能多 |
| **Unleashed**(第三方) | https://github.com/DarkFlippers/unleashed-firmware/releases | 定制强 |

下载后的固件文件格式是 `.dfu` 或 `.bin`,两种都支持。

> 📝 找对文件:在官方 Release 的 Assets 里,找 `flipper-z-f7-update-*.dfu`(Flipper 的主固件),不要下成 `any-remote-storage-update` 这种。

### 第 2 步:在 FlipperVM 里点「加载固件」

1. 软件主界面右上角「固件」区域,点 **加载固件 (.bin / .dfu)…**
2. 弹出文件选择框,选中刚才下载的 `.dfu` 或 `.bin` 文件
3. 成功的话,标签下方会显示:
   - 文件名
   - base(固件加载地址)、size(大小)
   - SP(初始栈地址)、Reset(启动入口)
4. 同时右侧 **UART 控制台**也会打印一行 `[loader] 已加载 ...`

### 第 3 步:点「▶ 运行」

1. 点右侧控制栏的 **▶ 运行** 按钮
2. 左侧 Flipper 屏幕会从「空白」逐步显示出开机画面
3. 开始玩吧!

---

## 🎮 操作指南(就像用真机一样)

### 按键对应表

Flipper 真机上有 6 个按键,你可以用**鼠标点击**或者**键盘**来操作:

| Flipper 按键 | 鼠标点哪个 | 电脑键盘 |
|---|---|---|
| ⬆ 上 | 屏幕下方的 ▲ | ↑ 方向键 |
| ⬇ 下 | ▼ | ↓ 方向键 |
| ⬅ 左 | ◀ | ← 方向键 |
| ➡ 右 | ▶ | → 方向键 |
| ✅ OK(中间) | OK 按钮 | **Enter / Return** |
| ↶ 返回(侧边) | ▶ Back | **Esc / Backspace** |

> 💡 小提示:在 Flipper 系统里,**OK 键是进入 / 确认,Back 键是退出 / 返回上一级**,这点和你用手机按「主页键 / 返回键」是一样的。

### 典型操作练习(建议你亲自试一遍)

假设已经进入 Flipper 主菜单:

| 你想做的事 | 怎么按 |
|---|---|
| 移动光标到「GPIO」 | 用方向键 ⬇⬇⬇... 或者 ➡ |
| 进入「GPIO」菜单 | 按 **OK** |
| 返回主菜单 | 按 **Back** |
| 回到桌面(如果在子菜单里) | 按好几次 **Back** |

### 速度调节

右侧「速度」滑条用于控制每帧执行多少条 CPU 指令:
- 太慢、画面卡顿:把滑条往右拉(每帧指令更多)
- 太快、操作不灵敏:往左拉

正常推荐值:50,000 – 100,000 条/帧。

### 其他控制按钮

| 按钮 | 作用 |
|---|---|
| ▶ 运行 | 开始执行固件 |
| ⏸ 暂停 | 暂停 CPU,方便查看屏幕 / 状态 |
| ⏭ 单步(1000) | 只执行 1000 条指令就暂停,用来排查卡死位置 |
| ⟲ 复位 | 相当于按真机的 RESET 按钮,重新启动固件 |

---

## 🔍 界面上的东西都是啥

一张图帮你看懂整个 GUI:

```
┌──────────────────────────┬───────────────────────────────┐
│  FLIPPER•ZERO  [VM]      │ 【固件】                       │
│  ┌────────────────────┐  │   [加载固件…]                  │
│  │                    │  │   base=... size=...           │
│  │   128×64 显示区    │  ├───────────────────────────────┤
│  │   (就是 Flipper 的 │  │ 【运行控制】                   │
│  │    屏幕内容)       │  │   ▶运行   ⏸暂停  ⏭单步  ⟲复位│
│  │                    │  ├───────────────────────────────┤
│  └────────────────────┘  │ 【速度】                       │
│      ▲                   │   约 50000 条/帧  [滑块]      │
│   ◀ OK ▶  按键区         ├───────────────────────────────┤
│      ▼                   │ 【状态】                       │
│   ◀ Back                 │   PC=...  SP=...  icount=...  │
│                          ├───────────────────────────────┤
│                          │ 【UART 控制台】                │
│                          │   (固件的调试日志都会打到这) │
└──────────────────────────┴───────────────────────────────┘
```

**UART 控制台**是个很有用的排错工具——固件内部用 `printf` 输出的所有信息都会显示在这儿。如果加载了固件却黑屏,先看控制台,通常能看到它卡在哪一步。

---

## ❓ 常见问题 FAQ

### Q1. 双击 `FlipperVM.bat` 后闪一下就没了,怎么办?

**原因**:程序启动时报了异常,但窗口关得太快你看不见。

**解决方法**:
1. 打开 **Windows 命令行(cmd)**:按 Win+R,输入 `cmd`,回车
2. 进入解压目录:比如 `cd /d D:\FlipperVM`
3. 手动运行:`python\python.exe app\main.py`
4. 这时候报错信息会停在屏幕上,你就能看清楚是啥问题了

最常见的两个原因:
- **解压路径包含中文 / 空格**:把文件夹移到纯英文路径,再试
- **没解压完整**:重下 zip,再解压一遍

### Q2. 为什么屏幕是黑的 / 没显示内容?

大多数情况下是因为**固件初始化某些本项目暂未仿真的外设(CC1101、NFC、BLE、红外等)时卡住了**。

排查方法:
1. 看 **UART 控制台**最后几行,看它停在什么阶段
2. 按 **⏸ 暂停**,再点 **⏭ 单步(1000)** 手动往前走几步,观察状态的 PC 变化
3. 去 [Issues](https://github.com/k20120509/flippervm/issues) 里贴控制台的内容,我们才能知道该补哪个外设

### Q3. 支持哪些 Flipper 外设?目前不支持哪些?

| 外设 | 支持情况 |
|---|---|
| ✅ 128×64 显示屏(ST7567) | 完整支持,实时渲染 |
| ✅ 6 个物理按键(GPIO + EXTI 中断) | 完整支持 |
| ✅ SysTick 滴答定时器 | 完整支持 |
| ✅ USART1 / USART2 调试串口 | 完整支持,控制台可见 |
| ✅ NVIC 中断控制器 | 基础支持(抢占关闭) |
| ✅ RCC 时钟 / PWR 电源 / GPIO | 可读写,关键位生效 |
| ✅ SPI2(显示控制器) | 完整支持 |
| 🔧 USB / 蓝牙 | 未仿真 |
| 🔧 CC1101 射频(Sub-GHz) | 未仿真 |
| 🔧 NFC / RFID | 未仿真 |
| 🔧 红外收发 | 未仿真 |
| 🔧 iButton 接触钥匙 | 未仿真 |
| 🔧 SD 卡 | 未仿真 |

> 上面「未仿真」的外设在读取时会返回 0 或触发未映射内存处理,所以**真实固件大概率跑不到菜单界面就卡住或黑屏**。本软件目前的主要用途是:**调试 / 逆向 / 研究固件启动流程**。想要完整功能?欢迎在 GitHub 提 Issue 或 PR 补齐外设,每次加一个外设固件就能往前走一步。

### Q4. 可以用来做什么?

- **学习 Flipper 固件**——官方固件源码庞大,仿真器里可以随时暂停、单步、看寄存器和 UART 输出
- **测试自制固件**——把自己编译的 `.bin` 丢进去快速验证是否会跑起来
- **逆向分析**——没有硬件的情况下研究攻击面

### Q5. 为什么便携版 zip 这么大(260 MB)?

里面打包了一整套 **Python 3.11 + PySide6 + Unicorn**(PySide6 的库文件就有 150MB+),所以你不用自己装任何东西。

未来如果通过 GitHub Actions PyInstaller 产出纯单文件 exe,体积会下降到 80–120MB 左右。

---

## 🛠 开发者章节

下面的内容只给想改源码 / 加外设 / 自己打包的同学看。小白用户可以忽略。

### 目录结构

```
flipper_vm/
  stm32wb55.py        STM32WB55 地址映射 & 按键 GPIO 配置表
  display.py          ST7567 显示控制器仿真(命令/数据流/帧缓冲)
  firmware_loader.py  DFU / bin 固件加载 & 向量表解析
  emulator.py         Unicorn 仿真核心 + 外设路由 + 异常/中断
  gui.py              PySide6 GUI(Flipper 机身/控制面板/UART 控制台)
main.py               入口(仅调用 gui.main())
requirements.txt      依赖列表
flippervm.spec        PyInstaller 配置(单文件 exe)
build.bat             Windows 一键打包脚本
test_smoke.py         冒烟测试(加载/跑最小固件/按键/显示)
test_systick.py       SysTick 异常进入 + 返回链路测试
```

### 添加一个新外设

以「TIM2 要加一个事件回调」为例:

1. 在 [emulator.py](flipper_vm/emulator.py) 的 `__init__` 里找到 `self.tim2 = Peripheral(...)`
2. 子类化 `Peripheral`,在 `read / write` 中实现你要的行为
3. 若有 IRQ,通过 `self._pend_irq(irq_no)` 注入 pending
4. 加冒烟测试,验证

### 用 GitHub Actions 自动构建 exe / 便携包

推一个 tag 即可:

```bash
git tag v0.3.0
git push --tags
```

Actions 会自动在 Windows runner 上跑,产出会自动附加到 Release:

| Job | 产物 |
|---|---|
| `build-exe` | `FlipperVM.exe`(PyInstaller 单文件) |
| `build-portable` | `FlipperVM-portable-win64.zip`(便携版) |

手动触发:打开 Actions 页 → Build Windows Release → Run workflow → 填版本号。

---

## 📝 Changelog

| 版本 | 说明 |
|---|---|
| v0.2.0 | 首次发布 Windows 便携版(免配置,解压即用);加入 Actions 工作流 |
| v0.1.0 | 首次发布源码(已移除) |

---

## 📄 许可

MIT License — 详见 [LICENSE](LICENSE)。

> 🚨 免责声明:本软件仅供学习与合法研究用途。请勿用于未经授权的设备复制、门禁 / 车钥匙 / 电视遥控等场景。请遵守当地法律法规。

---

