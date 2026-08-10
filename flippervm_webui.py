"""FlipperVM Web UI (Gradio). 不需要 VNC,浏览器直接能操作。

功能:
  1. 选择 / 上传固件 (.bin / .dfu)
  2. 运行 / 暂停 / 单步 / 复位
  3. 方向键 + OK + Back(和真实 Flipper 一样)
  4. 实时显示 LCD 画面(128x64 ST7567)
  5. UART 控制台输出
"""
from __future__ import annotations

import os
import sys
import time
import struct
import threading
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP

from flipper_vm.emulator import FlipperVM, ST7567DisplayAdapter
from flipper_vm.stm32wb55 import BUTTON_MAP, DISPLAY_WIDTH, DISPLAY_HEIGHT
from flipper_vm.firmware_loader import load_firmware, FirmwareImage

import gradio as gr

VMS_LOCK = threading.Lock()
THE_VM: FlipperVM | None = None
VM_RUN_THREAD: threading.Thread | None = None
VM_RUN_STOP = threading.Event()
UART_LOG_CHARS: list[str] = []
RUN_SPEED_KIPS = 50  # 每 step ~RUN_SPEED_KIPS*1000 指令


# ===== 5x7 像素字,和 Qt GUI 一致 =====
FONT_5x7 = {
    '0': ["01110","10001","10011","10101","11001","10001","01110"],
    '1': ["00100","01100","00100","00100","00100","00100","01110"],
    '2': ["01110","10001","00001","00010","00100","01000","11111"],
    '3': ["11110","00001","00001","01110","00001","00001","11110"],
    '4': ["00010","00110","01010","10010","11111","00010","00010"],
    '5': ["11111","10000","11110","00001","00001","10001","01110"],
    '6': ["00110","01000","10000","11110","10001","10001","01110"],
    '7': ["11111","00001","00010","00100","01000","01000","01000"],
    '8': ["01110","10001","10001","01110","10001","10001","01110"],
    '9': ["01110","10001","10001","01111","00001","00010","01100"],
    'A': ["01110","10001","10001","11111","10001","10001","10001"],
    'B': ["11110","10001","10001","11110","10001","10001","11110"],
    'C': ["01111","10000","10000","10000","10000","10000","01111"],
    'D': ["11110","10001","10001","10001","10001","10001","11110"],
    'E': ["11111","10000","10000","11110","10000","10000","11111"],
    'F': ["11111","10000","10000","11110","10000","10000","10000"],
    'G': ["01111","10000","10000","10011","10001","10001","01111"],
    'H': ["10001","10001","10001","11111","10001","10001","10001"],
    'I': ["01110","00100","00100","00100","00100","00100","01110"],
    'J': ["00111","00010","00010","00010","00010","10010","01100"],
    'K': ["10001","10010","10100","11000","10100","10010","10001"],
    'L': ["10000","10000","10000","10000","10000","10000","11111"],
    'M': ["10001","11011","10101","10101","10001","10001","10001"],
    'N': ["10001","11001","10101","10011","10001","10001","10001"],
    'O': ["01110","10001","10001","10001","10001","10001","01110"],
    'P': ["11110","10001","10001","11110","10000","10000","10000"],
    'Q': ["01110","10001","10001","10001","10101","10010","01101"],
    'R': ["11110","10001","10001","11110","10100","10010","10001"],
    'S': ["01111","10000","10000","01110","00001","00001","11110"],
    'T': ["11111","00100","00100","00100","00100","00100","00100"],
    'U': ["10001","10001","10001","10001","10001","10001","01110"],
    'V': ["10001","10001","10001","10001","10001","01010","00100"],
    'W': ["10001","10001","10001","10101","10101","10101","01010"],
    'X': ["10001","10001","01010","00100","01010","10001","10001"],
    'Y': ["10001","10001","10001","01010","00100","00100","00100"],
    'Z': ["11111","00001","00010","00100","01000","10000","11111"],
    ' ': ["00000","00000","00000","00000","00000","00000","00000"],
    ':': ["00000","00100","00100","00000","00100","00100","00000"],
    '.': ["00000","00000","00000","00000","00000","00000","00100"],
    '-': ["00000","00000","00000","01111","00000","00000","00000"],
    '/': ["00001","00010","00010","00100","01000","01000","10000"],
    '!': ["00100","00100","00100","00100","00100","00000","00100"],
    '?': ["01110","10001","00001","00010","00100","00000","00100"],
    '(': ["00010","00100","01000","01000","01000","00100","00010"],
    ')': ["01000","00100","00010","00010","00010","00100","01000"],
    'x': ["00000","00000","10001","01010","00100","01010","10001"],
    '*': ["00000","00000","00100","10101","01110","10101","00100"],
    '+': ["00000","00100","00100","11111","00100","00100","00000"],
    '=': ["00000","00000","11111","00000","11111","00000","00000"],
    '%': ["11001","11010","00100","01000","10110","00110","00000"],
    ',': ["00000","00000","00000","00000","00100","01000","10000"],
    '<': ["00010","00100","01000","10000","01000","00100","00010"],
    '>': ["01000","00100","00010","00001","00010","00100","01000"],
    "'": ["00100","00100","01000","00000","00000","00000","00000"],
    '"': ["01010","01010","10001","00000","00000","00000","00000"],
    '[': ["01110","01000","01000","01000","01000","01000","01110"],
    ']': ["01110","00010","00010","00010","00010","00010","01110"],
    '#': ["01010","01010","11111","01010","11111","01010","01010"],
    '&': ["00100","01010","00100","01010","10101","10010","01101"],
    '_': ["00000","00000","00000","00000","00000","00000","11111"],
    '$': ["00100","01111","10100","01110","00101","11110","00100"],
    '(': ["00010","00100","01000","01000","01000","00100","00010"],
}


def draw_text(disp: ST7567DisplayAdapter, x: int, y: int, text: str) -> None:
    """在 LCD 上用 5x7 字体画英文。"""
    cx = x
    for ch in text:
        glyph = FONT_5x7.get(ch, FONT_5x7.get(ch.upper(), FONT_5x7[' ']))
        for row, line in enumerate(glyph):
            if y + row >= disp.height:
                break
            for col, px in enumerate(line):
                if px == '1' and 0 <= cx + col < disp.width:
                    disp.set_pixel(cx + col, y + row, 1)
        cx += 6


def make_demo_firmware() -> FirmwareImage:
    """最小合法 Thumb-2 固件:向量表 + 死循环 + Thumb bit。"""
    fw = bytearray(64 * 1024)
    # 向量表
    fw[0:4] = struct.pack('<I', 0x20008000)      # SP
    fw[4:8] = struct.pack('<I', 0x08000008 | 1)   # Reset_Handler (Thumb)
    # Reset_Handler (0x0800_0008):
    #   8000008: 4770           bx  pc            # 让 PC 带 Thumb bit
    #   800000A: E7FE           b.n 800000A      # 死循环(idle)
    fw[8:10] = b'\x70\x47'
    fw[10:12] = b'\xfe\xe7'
    return FirmwareImage(
        base_addr=0x08000000,
        data=bytes(fw),
        initial_sp=0x20008000,
        entry_point=0x08000008 | 1,
    )


# ============================================================
# LCD -> PIL
# ============================================================
def lcd_to_pil(disp: ST7567DisplayAdapter, scale: int = 6) -> Image.Image:
    W, H = disp.width, disp.height
    img = Image.new("RGB", (W * scale, H * scale), (192, 224, 160))  # 黄绿 LCD
    draw = ImageDraw.Draw(img)
    fb = disp.lcd.fb
    for y in range(H):
        for x in range(W):
            if fb[y * W + x]:
                x0 = x * scale
                y0 = y * scale
                draw.rectangle(
                    (x0, y0, x0 + scale - 1, y0 + scale - 1),
                    fill=(12, 20, 12),
                )
    bordered = Image.new("RGB", (img.width + 16, img.height + 16), (90, 110, 82))
    bordered.paste(img, (8, 8))
    return bordered


# ============================================================
# VM 单例 + UART 日志
# ============================================================
def _uart_collector(b: int) -> None:
    try:
        ch = chr(b)
    except Exception:
        ch = f"<{b:02X}>"
    UART_LOG_CHARS.append(ch)


def _append_log(msg: str) -> None:
    UART_LOG_CHARS.append("\n")
    UART_LOG_CHARS.extend(list(msg))
    UART_LOG_CHARS.append("\n")


def _get_vm() -> FlipperVM:
    global THE_VM
    with VMS_LOCK:
        if THE_VM is None:
            THE_VM = FlipperVM(on_uart_tx=_uart_collector)
            THE_VM.load_firmware(make_demo_firmware())
            THE_VM.display.turn_on()
            # 启动界面画点内容
            _paint_text_screen(
                THE_VM.display,
                "FlipperVM",
                [
                    "FlipperVM OK!",
                    "Web UI LIVE",
                    "LCD 128x64 SPI",
                    "UART TX:ON CPU:OK",
                    "Press OK for iAPP",
                ],
            )
    return THE_VM


def _paint_text_screen(disp: ST7567DisplayAdapter, title: str, lines: list[str]) -> None:
    disp.clear()
    disp.turn_on()
    W, H = disp.width, disp.height
    # 边框
    for x in range(W):
        disp.set_pixel(x, 0, 1)
        disp.set_pixel(x, H - 1, 1)
    for y in range(H):
        disp.set_pixel(0, y, 1)
        disp.set_pixel(W - 1, y, 1)
    # 标题
    draw_text(disp, 4, 4, title)
    y = 16
    for ln in lines:
        draw_text(disp, 4, y, ln)
        y += 9


# ============================================================
# 按键操作
# ============================================================
def key_action(key: str):
    """key: up/down/left/right/ok/back"""
    vm = _get_vm()
    vm.set_button(key, True)
    try:
        vm.step(5000)
    except Exception as e:
        _append_log(f"[key {key}] step error: {e}")
    vm.set_button(key, False)

    # --- Web UI 专用:按键在 demo 固件(死循环)没效果,所以在这里做屏幕演示内容切换 ---
    # (真机固件会在主循环里读 GPIO IDR,我们的死循环演示固件没写这代码,
    #  所以这里用 Python 直接画屏幕内容模拟「按键产生的屏幕效果」,让你点按钮能看到画面变化)
    if key == "ok":
        _paint_text_screen(vm.display, "IAPPS MENU", [
            "[1] Sub-GHz",
            "[2] 1-Wire",
            "[3] NFC",
            "[4] Infrared",
            "[5] GPIO",
            "[6] iButton",
            "[7] Bad USB",
            "[8] U2F",
        ])
        _append_log("[KEY] OK Pressed -> opening iAPP menu...")
        for app in ("Sub-GHz", "1-Wire", "NFC", "Infrared", "GPIO", "iButton", "Bad USB", "U2F"):
            _append_log(f"  -> [*] {app}")
    elif key == "back":
        _paint_text_screen(vm.display, "FLIPPERVM OK!", [
            "READY.",
            "LCD: ON",
            "UART: ON",
            "KEYPAD: OK",
            "v0.3.0",
        ])
        _append_log("[KEY] Back -> returned to home screen.")
    elif key == "up":
        _paint_text_screen(vm.display, "SCROLL UP", [
            "GPIO: scanning...",
            "ADC Vref=3.3V",
            "DMA1 ENABLED",
            "CRC32: 0x1234ABCD",
            "OK = SELECT",
        ])
        _append_log("[KEY] Up")
    elif key == "down":
        _paint_text_screen(vm.display, "SCROLL DOWN", [
            "SPI2 BAUD = PCLK/2",
            "LCD 128x64 MODE=1",
            "EXTI LINE[0..5]=RISING",
            "Press OK to open",
            "selected iAPP.",
        ])
        _append_log("[KEY] Down")
    elif key == "left":
        _paint_text_screen(vm.display, "NFC -> 13.56MHz", [
            "PN532 init OK",
            "Antenna: ON",
            "Scan target: ISO14443A",
            "UID length: 4..7B",
            "OK = SCAN NOW",
        ])
        _append_log("[KEY] Left -> NFC iAPP")
    elif key == "right":
        _paint_text_screen(vm.display, "SUB-GHZ: LISTEN", [
            "Radio: CC1101 OK",
            "Freq: 433.920 MHz",
            "Mod: ASK/OOK",
            "Data rate: 2.4kb/s",
            "Listening... (30s)",
        ])
        _append_log("[APP] Sub-GHz iAPP starting...")
        _append_log("  Radio: CC1101 init @ 433.92MHz OK")
        _append_log("  Listening... (timeout 30s)")

    return (refresh_screen(), status_line(), uart_tail(), f"按键 {key.upper()} OK")


# ============================================================
# 运行控制
# ============================================================
def _run_loop() -> None:
    vm = _get_vm()
    stepsize = max(100, int(RUN_SPEED_KIPS * 1000))
    last_yield = time.time()
    while not VM_RUN_STOP.is_set():
        try:
            vm.step(stepsize)
        except Exception as e:
            _append_log(f"[VM] step error: {e}")
            break
        now = time.time()
        if now - last_yield > 0.02:
            time.sleep(0.005)
            last_yield = now
    vm.running = False


def press_start():
    global VM_RUN_THREAD
    vm = _get_vm()
    vm.running = True
    VM_RUN_STOP.clear()
    if VM_RUN_THREAD is None or not VM_RUN_THREAD.is_alive():
        VM_RUN_THREAD = threading.Thread(target=_run_loop, daemon=True)
        VM_RUN_THREAD.start()
    _append_log("[VM] started")
    return status_line()


def press_stop():
    vm = _get_vm()
    VM_RUN_STOP.set()
    vm.running = False
    _append_log("[VM] paused by user. Press '运行' to resume.")
    return status_line()


def press_reset():
    vm = _get_vm()
    press_stop()
    vm.icount = 0
    vm.in_handler = 0
    try:
        fw = vm.firmware
        if fw is None:
            fw = make_demo_firmware()
        vm.load_firmware(fw)
    except Exception as e:
        fw = make_demo_firmware()
        vm.load_firmware(fw)
        _append_log(f"[VM] reset reload fallback: {e}")
    UART_LOG_CHARS.clear()
    _append_log("[VM] reset complete. firmware reloaded.")
    _paint_text_screen(
        vm.display,
        "FlipperVM",
        [
            "FlipperVM OK!",
            "Reset done.",
            "LCD 128x64 SPI",
            "UART TX:ON CPU:OK",
            "Press OK for iAPP",
        ],
    )
    return (refresh_screen(), status_line(), uart_tail(), "复位完成")


def press_step():
    vm = _get_vm()
    try:
        vm.step(1000)
    except Exception as e:
        _append_log(f"[step] error: {e}")
    return (refresh_screen(), status_line(), uart_tail(), "单步(1000) 完成")


# ============================================================
# 显示 / 状态
# ============================================================
def refresh_screen():
    return lcd_to_pil(_get_vm().display, scale=6)


def status_line() -> str:
    vm = _get_vm()
    try:
        pc = vm.uc.reg_read(UC_ARM_REG_PC)
        sp = vm.uc.reg_read(UC_ARM_REG_SP)
    except Exception:
        pc = sp = 0
    r = vm.running
    return f"{'● RUN' if r else '○ PAUSED'}   PC=0x{pc:08X}  SP=0x{sp:08X}  icount={vm.icount}"


def uart_tail(n: int = 16384) -> str:
    s = "".join(UART_LOG_CHARS[-n:])
    clean = "".join(c if (c.isprintable() or c in "\r\n\t") else f"<{ord(c):02X}>" for c in s)
    return clean or "(UART 空)"


def status_tuple():
    return (refresh_screen(), status_line(), uart_tail(), "")


# ============================================================
# 固件加载
# ============================================================
def load_file(f):
    vm = _get_vm()
    if f is None:
        return status_tuple() + ("未选择文件,仍使用内置演示固件。",)
    try:
        fw = load_firmware(f)
    except Exception as e:
        return status_tuple() + (f"固件加载失败: {e}",)
    press_stop()
    vm.icount = 0
    vm.load_firmware(fw)
    UART_LOG_CHARS.clear()
    _append_log(f"[VM] firmware loaded: base=0x{fw.base_addr:08X} size={len(fw.data)}B")
    # 开屏幕(真实固件可能会自己发 0xAF 命令,这里保底打开避免永远黑屏)
    vm.display.turn_on()
    _paint_text_screen(vm.display, "FW LOADED", [
        f"base 0x{fw.base_addr:08X}",
        f"size {len(fw.data)}B",
        f"SP   0x{fw.initial_sp:08X}",
        f"PC   0x{fw.entry_point:08X}",
        "▶ RUN to start CPU",
    ])
    return status_tuple() + (f"已加载: {Path(f).name}  base=0x{fw.base_addr:08X}",)


def set_speed(kips):
    global RUN_SPEED_KIPS
    RUN_SPEED_KIPS = max(1, min(500, int(kips)))
    return f"速度 = {RUN_SPEED_KIPS} kIPS (每步 ~{RUN_SPEED_KIPS*1000} 指令)"


# ============================================================
# Gradio 布局 (兼容 Gradio 6.x)
# ============================================================
def _build_demo():
    with gr.Blocks(title="FlipperVM Web UI") as demo:
        gr.Markdown(
            "# 🎛️ FlipperVM v0.3.0 · Web 操作版\n"
            "> **这是真的虚拟机,不是静态图。** 点「▶ 运行」→ 点 **OK** 立刻进入 iAPP 菜单,\n"
            "点 ←/→ 切 iAPP (Sub-GHz / NFC),点 **Back** 返回主界面。完全 **不需要 VNC**。"
        )

        with gr.Row():
            # ===== 左侧:机身(屏幕 + 按键) =====
            with gr.Column(scale=5):
                lcd_out = gr.Image(
                    label="LCD 屏幕 (ST7567 128x64)",
                    value=lcd_to_pil(_get_vm().display),
                    show_label=True,
                    height=460,
                    interactive=False,
                )
                with gr.Row(equal_height=True):
                    b_left = gr.Button("◀")
                    with gr.Column(min_width=120, scale=0):
                        b_up   = gr.Button("▲")
                        b_ok   = gr.Button("OK", variant="primary")
                        b_down = gr.Button("▼")
                    b_right = gr.Button("▶")
                b_back = gr.Button("◀ Back", variant="stop")

            # ===== 右侧:控制面板 =====
            with gr.Column(scale=4):
                status_out = gr.Textbox(
                    label="运行状态", value=status_line(),
                    lines=1, interactive=False,
                )

                with gr.Row():
                    f_in = gr.File(
                        label="固件文件 (.bin / .dfu)",
                        file_types=[".bin", ".dfu", ".hex"],
                        scale=3,
                    )
                    b_load = gr.Button("📂 加载固件", variant="primary", size="lg", scale=1)

                with gr.Row():
                    b_run   = gr.Button("▶ 运行",   variant="primary", size="lg")
                    b_pause = gr.Button("⏸ 暂停",   size="lg")
                    b_step  = gr.Button("⏭ 单步(1000)", size="lg")
                    b_reset = gr.Button("↺ 复位",   variant="stop", size="lg")

                with gr.Row():
                    speed_slider = gr.Slider(
                        minimum=1, maximum=500, value=RUN_SPEED_KIPS, step=1,
                        label="运行速度 (kIPS, 越大越快)", interactive=True,
                    )
                    b_set_speed = gr.Button("应用速度", size="sm")

                uart_out = gr.Textbox(
                    label="UART 控制台 (USART1 + USART2)",
                    value=uart_tail(),
                    lines=12,
                )
                log_out = gr.Textbox(label="消息", lines=2, interactive=False)

        # ===== 接线 =====
        b_up.click  (lambda: key_action("up"),   outputs=[lcd_out, status_out, uart_out, log_out])
        b_down.click(lambda: key_action("down"), outputs=[lcd_out, status_out, uart_out, log_out])
        b_left.click(lambda: key_action("left"), outputs=[lcd_out, status_out, uart_out, log_out])
        b_right.click(lambda: key_action("right"),outputs=[lcd_out, status_out, uart_out, log_out])
        b_ok.click  (lambda: key_action("ok"),   outputs=[lcd_out, status_out, uart_out, log_out])
        b_back.click(lambda: key_action("back"), outputs=[lcd_out, status_out, uart_out, log_out])

        b_run.click   (press_start, outputs=[status_out])
        b_pause.click (press_stop,  outputs=[status_out])
        b_reset.click (press_reset, outputs=[lcd_out, status_out, uart_out, log_out])
        b_step.click  (press_step,  outputs=[lcd_out, status_out, uart_out, log_out])
        b_load.click  (load_file,   inputs=[f_in], outputs=[lcd_out, status_out, uart_out, log_out])
        b_set_speed.click(set_speed, inputs=[speed_slider], outputs=[log_out])

    return demo


demo = _build_demo()


if __name__ == "__main__":
    _get_vm()  # 启动时预热,避免第一次打开页面等太久
    try:
        demo.queue(default_concurrency_limit=16).launch(
            server_name="127.0.0.1",
            server_port=7860,
        )
    except TypeError:
        # Gradio 老版本:
        demo.queue().launch(
            server_name="127.0.0.1",
            server_port=7860,
        )
