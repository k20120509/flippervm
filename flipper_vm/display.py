"""ST7567 单色 LCD 控制器仿真(Flipper Zero 显示屏,128x64).

固件通过 SPI2 发送字节流:
  - DC=0:命令字节
  - DC=1:显示数据字节(每字节 8 行垂直像素)

关键命令:
  0xAE / 0xAF        : display off / on
  0x40|(line & 0x3F)  : set start line
  0xB0|(page & 0x0F)  : set page address(8 行/页,共 8 页)
  0x10|(colhi & 0x0F) : set column upper nibble
  0x00|(collo & 0x0F) : set column lower nibble
  0xA0 / 0xA1         : segment normal / reverse
  0xC0 / 0xC8         : common output normal / reverse
"""
from typing import List

from .stm32wb55 import DISPLAY_WIDTH, DISPLAY_HEIGHT


class ST7567:
    def __init__(self) -> None:
        # 1 字节/像素,0=灭,1=亮,方便 GUI 直接渲染
        self.fb: List[int] = [0] * (DISPLAY_WIDTH * DISPLAY_HEIGHT)
        self.display_on = False
        self.page = 0
        self.column = 0
        self.dc = 0          # 0=cmd,1=data(由 PB11 ODR 驱动)
        self.rst_active = False  # 由 PC9 ODR 驱动(低有效)
        self.segment_reverse = False
        self.common_reverse = False
        self.start_line = 0
        self._dirty = True

    # --- 外部信号 ---
    def set_dc(self, value: int) -> None:
        self.dc = 1 if value else 0

    def set_reset(self, active: bool) -> None:
        if active and not self.rst_active:
            self._reset_controller()
        self.rst_active = active

    def _reset_controller(self) -> None:
        self.display_on = False
        self.page = 0
        self.column = 0
        self.segment_reverse = False
        self.common_reverse = False
        self.start_line = 0
        self.fb = [0] * (DISPLAY_WIDTH * DISPLAY_HEIGHT)
        self._dirty = True

    # --- SPI 字节入口 ---
    def spi_write(self, data_byte: int) -> None:
        if self.rst_active:
            return
        if self.dc == 0:
            self._handle_command(data_byte & 0xFF)
        else:
            self._handle_data(data_byte & 0xFF)

    def _handle_command(self, cmd: int) -> None:
        if cmd in (0xAE, 0xAF):
            self.display_on = (cmd == 0xAF)
        elif 0x40 <= cmd <= 0x7F:
            self.start_line = cmd & 0x3F
        elif 0xB0 <= cmd <= 0xB7:
            self.page = cmd & 0x07
        elif 0x10 <= cmd <= 0x1F:
            self.column = (self.column & 0x0F) | ((cmd & 0x0F) << 4)
        elif 0x00 <= cmd <= 0x0F:
            self.column = (self.column & 0xF0) | (cmd & 0x0F)
        elif cmd == 0xA0:
            self.segment_reverse = False
        elif cmd == 0xA1:
            self.segment_reverse = True
        elif cmd == 0xC0:
            self.common_reverse = False
        elif cmd == 0xC8:
            self.common_reverse = True
        # 其它命令(电源/对比度等)对显示无影响,忽略即可

    def _handle_data(self, data: int) -> None:
        page = self.page
        col = self.column
        if 0 <= page < (DISPLAY_HEIGHT // 8) and 0 <= col < DISPLAY_WIDTH:
            for bit in range(8):
                row = page * 8 + bit
                if row >= DISPLAY_HEIGHT:
                    break
                # ST7567 bit0 = 上方像素
                pixel = (data >> bit) & 1
                if self.common_reverse:
                    row = DISPLAY_HEIGHT - 1 - row
                draw_col = (DISPLAY_WIDTH - 1 - col) if self.segment_reverse else col
                self.fb[row * DISPLAY_WIDTH + draw_col] = pixel
        self.column = (self.column + 1) & 0x7F
        self._dirty = True

    # --- 给 GUI 用 ---
    def framebuffer(self):
        return self.fb

    @property
    def dirty(self) -> bool:
        return self._dirty

    def clear_dirty(self) -> None:
        self._dirty = False
