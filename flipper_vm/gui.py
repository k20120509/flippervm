"""FlipperVM GUI - PySide6 实现.

布局:
  左侧:Flipper Zero 风格机身(128x64 OLED + 方向键 + OK + Back)
  右侧:控制面板(加载固件 / 运行 / 暂停 / 复位 / 单步)+ UART 控制台 + 状态
"""
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QPushButton, QVBoxLayout,
    QHBoxLayout, QLabel, QPlainTextEdit, QSlider, QGroupBox, QFrame,
)
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP

from .emulator import FlipperVM
from .firmware_loader import load_firmware
from .stm32wb55 import DISPLAY_WIDTH, DISPLAY_HEIGHT


# 键盘 → Flipper 按键映射
KEY_MAP = {
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
    Qt.Key_Return: "ok",
    Qt.Key_Enter: "ok",
    Qt.Key_Escape: "back",
    Qt.Key_Backspace: "back",
}


class DisplayWidget(QWidget):
    """128x64 OLED 渲染。每个像素放大 4 倍。"""
    SCALE = 4

    def __init__(self, vm: FlipperVM):
        super().__init__()
        self.vm = vm
        self.setFixedSize(DISPLAY_WIDTH * self.SCALE, DISPLAY_HEIGHT * self.SCALE)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("#9cA0a8"))
        self.setPalette(pal)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#3a3f4b"))  # 机身边框
        inner_x = (self.width() - DISPLAY_WIDTH * self.SCALE) // 2
        inner_y = (self.height() - DISPLAY_HEIGHT * self.SCALE) // 2
        # 屏幕背景
        p.fillRect(inner_x, inner_y,
                   DISPLAY_WIDTH * self.SCALE, DISPLAY_HEIGHT * self.SCALE,
                   QColor("#7da14a"))  # 经典绿黑 LCD
        if not self.vm.display.display_on:
            # 即使 display_on=关,仍然把 framebuffer 内容画出来,
            # 但使用较暗的颜色提示"屏幕关闭",避免用户完全看不到任何反馈。
            # 真正关闭时(例如 LCD reset 中),framebuffer 通常也是全 0,画面自然是一片空。
            pass
        fb = self.vm.display.fb
        # 点亮像素颜色:屏幕开启用深绿,关闭状态用半透明灰色
        pixel_color = QColor("#0d1f0a") if self.vm.display.display_on else QColor("#2a3a25")
        for y in range(DISPLAY_HEIGHT):
            for x in range(DISPLAY_WIDTH):
                if fb[y * DISPLAY_WIDTH + x]:
                    p.fillRect(inner_x + x * self.SCALE,
                               inner_y + y * self.SCALE,
                               self.SCALE, self.SCALE,
                               pixel_color)
        p.end()


class ButtonPad(QWidget):
    """方向键 + OK + Back。"""
    button_pressed = Signal(str, bool)  # name, pressed

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # 上
        row = QHBoxLayout()
        row.addStretch()
        self.up = self._make_btn("▲", "up")
        row.addWidget(self.up)
        row.addStretch()
        layout.addLayout(row)

        # 左 OK 右
        row = QHBoxLayout()
        self.left = self._make_btn("◀", "left")
        self.ok = self._make_btn("OK", "ok")
        self.ok.setMinimumWidth(72)
        self.right = self._make_btn("▶", "right")
        row.addWidget(self.left)
        row.addWidget(self.ok)
        row.addWidget(self.right)
        layout.addLayout(row)

        # 下
        row = QHBoxLayout()
        row.addStretch()
        self.down = self._make_btn("▼", "down")
        row.addWidget(self.down)
        row.addStretch()
        layout.addLayout(row)

        # Back
        row = QHBoxLayout()
        row.addStretch()
        self.back = self._make_btn("◀ Back", "back")
        row.addWidget(self.back)
        row.addStretch()
        layout.addLayout(row)

    def _make_btn(self, label: str, name: str) -> QPushButton:
        b = QPushButton(label)
        b.setFixedSize(56, 44)
        b.setStyleSheet("QPushButton { font-weight: bold; font-size: 14px; }")
        b.pressed.connect(lambda n=name: self.button_pressed.emit(n, True))
        b.released.connect(lambda n=name: self.button_pressed.emit(n, False))
        return b


class ConsoleWidget(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from flipper_vm._version import APP_TITLE
        self.setWindowTitle(APP_TITLE)
        self.resize(900, 640)

        self.vm = FlipperVM(on_uart_tx=self._on_uart_byte)
        self.running = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # === 左侧:机身 ===
        body = QFrame()
        body.setFrameShape(QFrame.StyledPanel)
        body.setStyleSheet("QFrame { background-color: #f4a13a; border-radius: 18px; }")
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(20, 20, 20, 20)
        body_l.setSpacing(14)

        title = QLabel("FLIPPER•ZERO  [VM]")
        title.setStyleSheet("color: #2a2a2a; font-weight: bold; font-size: 14px;")
        title.setAlignment(Qt.AlignCenter)
        body_l.addWidget(title)

        self.display = DisplayWidget(self.vm)
        body_l.addWidget(self.display, alignment=Qt.AlignCenter)

        self.pad = ButtonPad()
        self.pad.button_pressed.connect(self._on_button)
        body_l.addWidget(self.pad)

        root.addWidget(body, stretch=2)

        # === 右侧:控制 ===
        ctrl = QWidget()
        ctrl_l = QVBoxLayout(ctrl)
        ctrl_l.setContentsMargins(8, 8, 8, 8)

        # 固件
        fw_grp = QGroupBox("固件")
        fw_l = QVBoxLayout(fw_grp)
        btn_load = QPushButton("加载固件 (.bin / .dfu)…")
        btn_load.clicked.connect(self._load_firmware)
        self.lbl_fw = QLabel("未加载")
        self.lbl_fw.setWordWrap(True)
        fw_l.addWidget(btn_load)
        fw_l.addWidget(self.lbl_fw)
        ctrl_l.addWidget(fw_grp)

        # 运行控制
        run_grp = QGroupBox("运行控制")
        run_l = QHBoxLayout(run_grp)
        self.btn_run = QPushButton("▶ 运行")
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_step = QPushButton("⏭ 单步(1000)")
        self.btn_reset = QPushButton("⟲ 复位")
        for b in (self.btn_run, self.btn_pause, self.btn_step, self.btn_reset):
            run_l.addWidget(b)
        self.btn_run.clicked.connect(self._start)
        self.btn_pause.clicked.connect(self._pause)
        self.btn_step.clicked.connect(self._step_once)
        self.btn_reset.clicked.connect(self._reset)
        ctrl_l.addWidget(run_grp)

        # 速度
        spd_grp = QGroupBox("速度")
        spd_l = QVBoxLayout(spd_grp)
        self.lbl_spd = QLabel("约 50000 条/帧")
        self.sld_spd = QSlider(Qt.Horizontal)
        self.sld_spd.setRange(1, 50)
        self.sld_spd.setValue(10)
        self.sld_spd.valueChanged.connect(self._on_speed)
        spd_l.addWidget(self.lbl_spd)
        spd_l.addWidget(self.sld_spd)
        ctrl_l.addWidget(spd_grp)

        # 状态
        status_grp = QGroupBox("状态")
        st_l = QVBoxLayout(status_grp)
        self.lbl_status = QLabel("PC=0x00000000  SP=0x00000000  icount=0")
        st_l.addWidget(self.lbl_status)
        ctrl_l.addWidget(status_grp)

        # UART
        console_grp = QGroupBox("UART 控制台(USART1/2 输出)")
        cl = QVBoxLayout(console_grp)
        self.console = ConsoleWidget()
        cl.addWidget(self.console)
        ctrl_l.addWidget(console_grp, stretch=1)

        root.addWidget(ctrl, stretch=1)

        # 定时器:每 16ms 跑一帧
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.instructions_per_frame = 50000

        # 卡死检测:记录最近 64 次 tick 的 (PC, icount), 如果 PC 在很小范围内跳变
        # (例如同一条 B . 或一个 while 循环 2~3 条指令)且 icount 增长过慢 10 秒
        # -> 报警 "卡死" 并输出函数名
        self._stuck_pc_window = []   # list of (pc, icount, ts_ms)
        self._stuck_warned_pc = None   # 已报警过的 PC,避免刷屏

    # ---------- 固件 ----------
    def _load_firmware(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Flipper Zero 固件", "",
            "固件文件 (*.bin *.dfu);;所有文件 (*)")
        if not path:
            return
        try:
            fw = load_firmware(path)
        except Exception as e:
            self.console.appendPlainText(f"[loader] 加载失败: {e}")
            return
        self.vm.load_firmware(fw)
        self.lbl_fw.setText(
            f"{Path(path).name}\n"
            f"  base=0x{fw.base_addr:08X}  size={len(fw.data)} 字节\n"
            f"  SP=0x{fw.initial_sp:08X}\n"
            f"  Reset=0x{fw.entry_point:08X}")
        self.console.appendPlainText(
            f"[loader] 已加载 {Path(path).name},SP=0x{fw.initial_sp:08X},"
            f"Reset=0x{fw.entry_point:08X}")
        self._refresh_status()

    # ---------- 运行控制 ----------
    def _start(self):
        if self.vm.firmware is None:
            self.console.appendPlainText("[vm] 请先加载固件")
            return
        self.running = True
        self.timer.start()

    def _pause(self):
        self.running = False
        self.timer.stop()

    def _step_once(self):
        if self.vm.firmware is None:
            return
        try:
            self.vm.step(1000)
        except Exception as e:
            self.console.appendPlainText(f"[vm] 单步异常: {e}")
            # 自愈:尝试复位重启,让用户能继续操作
            try:
                if self.vm.firmware is not None:
                    self.vm.load_firmware(self.vm.firmware)
                    self.console.appendPlainText("[vm] 单步异常后已自动复位")
            except Exception:
                self._pause()
        self.display.update()
        self._refresh_status()

    def _reset(self):
        self._pause()
        if self.vm.firmware is not None:
            # 重新加载固件到 Flash
            self.vm.uc.mem_write(0x08000000, self.vm.firmware.data)
            self.vm.load_firmware(self.vm.firmware)
        self.vm.icount = 0
        self.vm.in_handler = 0
        self.console.appendPlainText("[vm] 已复位")

    def _tick(self):
        if not self.running:
            return
        try:
            self.vm.step(self.instructions_per_frame)
        except Exception as e:
            # Windows 自愈:Unicorn 在 Windows 上偶发 64 位地址访问违规,
            # step() 内部已尝试恢复 (跳过指令或重置到 Reset_Handler)。
            # 如果还是抛到这里,再尝试一次完整复位 + 续跑,
            # 而不是直接暂停让用户黑屏。
            self.console.appendPlainText(f"[vm] 运行异常: {e}")
            try:
                if self.vm.firmware is not None:
                    self.vm.load_firmware(self.vm.firmware)
                    self.console.appendPlainText("[vm] 已自动复位重启")
                    # 不暂停,继续运行 —— 让用户看到画面
            except Exception as e2:
                self.console.appendPlainText(f"[vm] 自动复位失败: {e2}")
                self._pause()
        # 无论是否异常,都刷新屏幕 —— 用户要"不断检测画面"
        self.display.update()
        self._refresh_status()
        # 卡死检测
        import time
        pc = self.vm.uc.reg_read(UC_ARM_REG_PC)
        self._stuck_pc_window.append((pc, self.vm.icount, int(time.monotonic() * 1000)))
        if len(self._stuck_pc_window) > 64:
            self._stuck_pc_window.pop(0)
        if len(self._stuck_pc_window) >= 32:
            first_pc, first_ic, first_ts = self._stuck_pc_window[0]
            last_pc, last_ic, last_ts = self._stuck_pc_window[-1]
            if (last_ts - first_ts) > 8000 and (last_ic - first_ic) < 200 * len(self._stuck_pc_window):
                # 8 秒内 icount 增长低于 ~200 条/tick => 疑似死循环
                pcs = {p for p, _, _ in self._stuck_pc_window}
                min_pc, max_pc = min(pcs), max(pcs)
                # 只对同一块卡死区域报警一次,避免满屏警告
                if self._stuck_warned_pc != min_pc:
                    self._stuck_warned_pc = min_pc
                    self.console.appendPlainText(
                        f"[WARN] ⚠️ 可能卡死: PC 在 0x{min_pc:08X}..0x{max_pc:08X} 循环 8s 以上, "
                        f"涉及 {len(pcs)} 条指令。\n"
                        f"[WARN]   可能是:等待某个未仿真的外设就绪 / Flash 等待 CPU2 / 中断未响应。\n"
                        f"[WARN]   请把 UART 日志贴给开发者,以便补全仿真。")

    def _on_speed(self, v: int):
        self.instructions_per_frame = v * 5000
        self.lbl_spd.setText(f"约 {self.instructions_per_frame} 条/帧")

    def _refresh_status(self):
        pc = self.vm.uc.reg_read(UC_ARM_REG_PC)
        sp = self.vm.uc.reg_read(UC_ARM_REG_SP)
        self.lbl_status.setText(
            f"PC=0x{pc:08X}  SP=0x{sp:08X}  icount={self.vm.icount}  "
            f"handler={self.vm.in_handler}")

    # ---------- 按键 ----------
    def _on_button(self, name: str, pressed: bool):
        self.vm.set_button(name, pressed)

    def keyPressEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        if event.key() in KEY_MAP:
            self.vm.set_button(KEY_MAP[event.key()], True)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        if event.key() in KEY_MAP:
            self.vm.set_button(KEY_MAP[event.key()], False)
            return
        super().keyReleaseEvent(event)

    # ---------- UART ----------
    def _on_uart_byte(self, b: int):
        ch = chr(b) if 32 <= b < 127 else (chr(b) if b in (9, 10, 13) else "")
        if ch:
            self.console.moveCursor(QTextCursor.MoveOperation.End)
            self.console.insertPlainText(ch)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
