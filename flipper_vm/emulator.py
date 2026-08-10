"""FlipperVM 仿真核心.

封装 Unicorn(ARM Cortex-M4)并挂载 STM32WB55 外设:
  Flash / SRAM / System memory 直接映射
  外设区通过 READ/WRITE 钩子分发到各外设对象

支持的异常:Reset / SysTick / NVIC(按键 EXTI 可注入)
"""
import struct
from typing import Callable, Optional

from unicorn import (
    Uc, UC_ARCH_ARM, UC_MODE_MCLASS, UC_MODE_THUMB,
    UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
    UC_HOOK_MEM_READ_UNMAPPED, UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_HOOK_INTR, UC_ERR_OK, UC_PROT_ALL,
)
from unicorn.arm_const import (
    UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R12, UC_ARM_REG_CPSR, UC_ARM_REG_XPSR,
)

from .stm32wb55 import (
    FLASH_BASE, FLASH_SIZE, SRAM1_BASE, SRAM1_SIZE, SRAM2_BASE, SRAM2_SIZE,
    SYSTEM_MEM_BASE, SYSTEM_MEM_SIZE, PERIPH_BASE, PPB_BASE,
    RCC_BASE, PWR_BASE, FLASH_REG_BASE, RNG_BASE, EXTI_BASE,
    DMA1_BASE, DMAMUX1_BASE, CRC_BASE, GPIOA_BASE, GPIOB_BASE, GPIOC_BASE,
    GPIOD_BASE, GPIOH_BASE, ADC_BASE, SPI2_BASE, USART1_BASE, USART2_BASE,
    TIM2_BASE, I2C1_BASE, AES1_BASE, AES2_BASE,
    SYSTICK_BASE, SCB_BASE, NVIC_BASE, SCB_VTOR,
    BUTTON_MAP,
)
from .display import ST7567
from .firmware_loader import FirmwareImage


# Cortex-M 系统异常号
EXC_RESET = 1
EXC_SVCALL = 11
EXC_PENDSV = 14
EXC_SYSTICK = 15
IRQ0_OFFSET = 16  # IRQ0 -> exception 16


# ====== 通用外设基类 ======
class Peripheral:
    """简单寄存器透传外设:大部分寄存器读返回写入值,可被子类覆盖。"""
    def __init__(self, base: int, size: int, name: str = ""):
        self.base = base
        self.size = size
        self.name = name or self.__class__.__name__
        self.regs = bytearray(size)

    def read(self, offset: int, size: int) -> int:
        return int.from_bytes(self.regs[offset:offset + size], "little")

    def write(self, offset: int, size: int, value: int) -> None:
        self.regs[offset:offset + size] = value.to_bytes(size, "little")[:size]


class GPIO(Peripheral):
    """STM32 GPIO。按键位通过外部状态注入 IDR。"""
    IDR = 0x10
    ODR = 0x14
    BSRR = 0x18
    BRR = 0x28

    def __init__(self, base: int, name: str):
        super().__init__(base, 0x30, name)
        # 按键位掩码(由 FlipperVM.register_buttons 设置)
        self.button_mask = 0
        # 按键当前按下状态(0=按下,1=释放,因上拉输入)
        self.button_pressed = {}  # pin_bit -> bool

    def read(self, offset, size):
        if offset == self.IDR:
            # 默认所有按键位为 1(上拉),按下时为 0
            val = 0xFFFFFFFF
            for pin_bit, pressed in self.button_pressed.items():
                if pressed:
                    val &= ~pin_bit
                else:
                    val |= pin_bit
            # ODR 中的位也并入 IDR(开漏输出回读)
            val |= int.from_bytes(self.regs[self.ODR:self.ODR + 4], "little") & 0xFFFF
            return val & 0xFFFF
        return super().read(offset, size)

    def write(self, offset, size, value):
        if offset == self.BSRR:
            odr = int.from_bytes(self.regs[self.ODR:self.ODR + 4], "little")
            if value & 0xFFFF0000:
                odr &= ~((value >> 16) & 0xFFFF)
            odr |= value & 0xFFFF
            self.regs[self.ODR:self.ODR + 4] = (odr & 0xFFFF).to_bytes(4, "little")
            self._on_odr_change(odr)
            return
        if offset == self.BRR:
            odr = int.from_bytes(self.regs[self.ODR:self.ODR + 4], "little")
            odr &= ~(value & 0xFFFF)
            self.regs[self.ODR:self.ODR + 4] = (odr & 0xFFFF).to_bytes(4, "little")
            self._on_odr_change(odr)
            return
        if offset == self.ODR:
            super().write(offset, size, value)
            self._on_odr_change(value)
            return
        # 其它寄存器(MODER / OTYPER / OSPEEDR / PUPDR / AFRL / AFRH 等)
        # 固件初始化时通常按 MODER -> ODR/BSRR 顺序配置,只写 MODER 时不会触发
        # DC/RST 钩子,这里在写完其它寄存器后也主动回调一次,确保 ODR 的当前值被推送到外设。
        super().write(offset, size, value)
        odr = int.from_bytes(self.regs[self.ODR:self.ODR + 4], "little")
        self._on_odr_change(odr)

    def _on_odr_change(self, odr):
        """子类钩子:用于检测 DC / RST 引脚变化。"""

    def set_button(self, pin_bit: int, pressed: bool):
        self.button_pressed[pin_bit] = pressed


class GPIOB(GPIO):
    """PB4=Left, PB5=OK 按键;PB11=显示 DC。"""
    def __init__(self, base, name, display: ST7567):
        super().__init__(base, name)
        self.display = display
        self.dc_bit = 1 << 11

    def _on_odr_change(self, odr):
        self.display.set_dc(1 if (odr & self.dc_bit) else 0)


class GPIOC(GPIO):
    """PC0=Back, PC1=Down, PC2=Up, PC3=Right 按键;PC9=显示 RST(低有效)。"""
    def __init__(self, base, name, display: ST7567):
        super().__init__(base, name)
        self.display = display
        self.rst_bit = 1 << 9

    def _on_odr_change(self, odr):
        self.display.set_reset(not bool(odr & self.rst_bit))


class SPI2(Peripheral):
    """SPI2 用于驱动 ST7567 显示屏。每写入 DR 一个字节即送入显示控制器。"""
    CR1 = 0x00
    CR2 = 0x04
    SR = 0x08
    DR = 0x0C
    SR_TXE = 1 << 1   # TX empty
    SR_RXNE = 1 << 0  # RX not empty
    SR_BSY = 1 << 7

    def __init__(self, base, name, display: ST7567):
        super().__init__(base, 0x30, name)
        self.display = display
        # 默认 TX 空,RX 空
        self._sr = self.SR_TXE
        self.regs[self.SR:self.SR + 4] = self._sr.to_bytes(4, "little")

    def read(self, offset, size):
        if offset == self.SR:
            return self._sr & 0xFF if size == 1 else self._sr & 0xFFFF
        if offset == self.DR:
            # 读 DR 通常清 RXNE
            self._sr &= ~self.SR_RXNE
            self.regs[self.SR:self.SR + 4] = self._sr.to_bytes(4, "little")
            return 0
        return super().read(offset, size)

    def write(self, offset, size, value):
        if offset == self.DR:
            self.display.spi_write(value & 0xFF)
            # TX 总是空,模拟非阻塞发送
            return
        super().write(offset, size, value)
        if offset == self.SR:
            self._sr = value


class USART(Peripheral):
    """USART1 用于 LPUART 调试输出。固件写 DR 即把字节送到 callback。"""
    CR1 = 0x00
    ISR = 0x1C
    TDR = 0x28
    ISR_TXE = 1 << 7
    ISR_TC = 1 << 6

    def __init__(self, base, name, on_tx: Optional[Callable[[int], None]] = None):
        super().__init__(base, 0x40, name)
        self.on_tx = on_tx
        self._isr = self.ISR_TXE | self.ISR_TC
        self.regs[self.ISR:self.ISR + 4] = self._isr.to_bytes(4, "little")

    def read(self, offset, size):
        if offset == self.ISR:
            return self._isr
        return super().read(offset, size)

    def write(self, offset, size, value):
        if offset == self.TDR:
            if self.on_tx:
                self.on_tx(value & 0xFF)
            return
        super().write(offset, size, value)


class ST7567DisplayAdapter:
    """把 ST7567 包装成有 framebuffer 接口的对象。"""
    def __init__(self):
        from .stm32wb55 import DISPLAY_WIDTH, DISPLAY_HEIGHT
        self.lcd = ST7567()
        self.width  = DISPLAY_WIDTH
        self.height = DISPLAY_HEIGHT

    @property
    def fb(self):
        return self.lcd.fb

    @property
    def display_on(self):
        return self.lcd.display_on

    @property
    def dirty(self):
        return self.lcd.dirty

    def clear_dirty(self):
        self.lcd.clear_dirty()

    def set_dc(self, v): self.lcd.set_dc(v)
    def set_reset(self, a): self.lcd.set_reset(a)
    def spi_write(self, b): self.lcd.spi_write(b)

    # --- 方便 GUI 调试 / 测试脚本直接画像素 ---
    def clear(self) -> None:
        self.lcd.fb = [0] * (self.width * self.height)
        self.lcd._dirty = True

    def set_pixel(self, x: int, y: int, on: int) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.lcd.fb[y * self.width + x] = 1 if on else 0
            self.lcd._dirty = True

    def turn_on(self) -> None:
        """打开 LCD 显示(发 ST7567 0xAF 命令),让 GUI 画像素时真的能看到。"""
        self.lcd.display_on = True
        self.lcd._dirty = True

    def turn_off(self) -> None:
        self.lcd.display_on = False
        self.lcd._dirty = True


# ====== 仿真核心 ======
class FlipperVM:
    # 外设区与 PPB 区映射范围
    PERIPH_REGION_BASE = 0x40000000
    PERIPH_REGION_SIZE = 0x20000000   # 512MB,覆盖 AHB/APB
    PPB_REGION_BASE = 0xE0000000
    PPB_REGION_SIZE = 0x100000        # 1MB

    def __init__(self, on_uart_tx: Optional[Callable[[int], None]] = None):
        self.on_uart_tx = on_uart_tx
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_MCLASS | UC_MODE_THUMB)
        self.display = ST7567DisplayAdapter()

        # 内存映射
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE, UC_PROT_ALL)
        self.uc.mem_map(SRAM1_BASE, SRAM1_SIZE + SRAM2_SIZE, UC_PROT_ALL)
        self.uc.mem_map(SYSTEM_MEM_BASE, SYSTEM_MEM_SIZE + 0x1000, UC_PROT_ALL)
        self.uc.mem_map(self.PERIPH_REGION_BASE, self.PERIPH_REGION_SIZE, UC_PROT_ALL)
        self.uc.mem_map(self.PPB_REGION_BASE, self.PPB_REGION_SIZE, UC_PROT_ALL)

        # 外设实例化
        self.gpioa = GPIO(GPIOA_BASE, "GPIOA")
        self.gpiob = GPIOB(GPIOB_BASE, "GPIOB", self.display.lcd)
        self.gpioc = GPIOC(GPIOC_BASE, "GPIOC", self.display.lcd)
        self.gpiod = GPIO(GPIOD_BASE, "GPIOD")
        self.gpioh = GPIO(GPIOH_BASE, "GPIOH")
        self.spi2 = SPI2(SPI2_BASE, "SPI2", self.display.lcd)
        self.usart1 = USART(USART1_BASE, "USART1", on_uart_tx)
        self.usart2 = USART(USART2_BASE, "USART2", on_uart_tx)
        self.exti = Peripheral(EXTI_BASE, 0x80, "EXTI")
        self.rcc = Peripheral(RCC_BASE, 0x400, "RCC")
        self.pwr = Peripheral(PWR_BASE, 0x100, "PWR")
        self.flash_reg = Peripheral(FLASH_REG_BASE, 0x100, "FLASH")
        self.rng = Peripheral(RNG_BASE, 0x100, "RNG")
        self.dma1 = Peripheral(DMA1_BASE, 0x400, "DMA1")
        self.dmamux = Peripheral(DMAMUX1_BASE, 0x100, "DMAMUX1")
        self.crc = Peripheral(CRC_BASE, 0x40, "CRC")
        self.adc = Peripheral(ADC_BASE, 0x400, "ADC")
        self.aes1 = Peripheral(AES1_BASE, 0x400, "AES1")
        self.aes2 = Peripheral(AES2_BASE, 0x400, "AES2")
        self.tim2 = Peripheral(TIM2_BASE, 0x400, "TIM2")
        self.i2c1 = Peripheral(I2C1_BASE, 0x400, "I2C1")

        # PPB 区域(直接字节数组存)
        self.ppb = bytearray(self.PPB_REGION_SIZE)

        # 外设分发表
        self.peripherals = {
            GPIOA_BASE: self.gpioa, GPIOB_BASE: self.gpiob, GPIOC_BASE: self.gpioc,
            GPIOD_BASE: self.gpiod, GPIOH_BASE: self.gpioh,
            SPI2_BASE: self.spi2, USART1_BASE: self.usart1, USART2_BASE: self.usart2,
            EXTI_BASE: self.exti, RCC_BASE: self.rcc, PWR_BASE: self.pwr,
            FLASH_REG_BASE: self.flash_reg, RNG_BASE: self.rng,
            DMA1_BASE: self.dma1, DMAMUX1_BASE: self.dmamux, CRC_BASE: self.crc,
            ADC_BASE: self.adc, AES1_BASE: self.aes1, AES2_BASE: self.aes2,
            TIM2_BASE: self.tim2, I2C1_BASE: self.i2c1,
        }

        # 按键 GPIO 注册
        for name, (gpio_base, pin_bit) in BUTTON_MAP.items():
            self.peripherals[gpio_base].set_button(pin_bit, False)

        # 状态
        self.firmware: Optional[FirmwareImage] = None
        self.running = False
        self.icount = 0
        self.systick_load = 0
        self.systick_ctrl = 0
        self.systick_val = 0
        self.systick_countdown = 0
        self.in_handler = 0   # 当前处于异常处理层数
        self.nvic_iser = [0, 0, 0, 0]   # 32-bit ×4 = 128 IRQs enable
        self.nvic_pending = [0, 0, 0, 0]
        self.nvic_active = [0, 0, 0, 0]

        # 安装钩子
        self.uc.hook_add(UC_HOOK_MEM_READ, self._hook_mem_read,
                         begin=self.PERIPH_REGION_BASE,
                         end=self.PERIPH_REGION_BASE + self.PERIPH_REGION_SIZE - 1)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._hook_mem_write,
                         begin=self.PERIPH_REGION_BASE,
                         end=self.PERIPH_REGION_BASE + self.PERIPH_REGION_SIZE - 1)
        self.uc.hook_add(UC_HOOK_MEM_READ, self._hook_ppb_read,
                         begin=self.PPB_REGION_BASE,
                         end=self.PPB_REGION_BASE + self.PPB_REGION_SIZE - 1)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._hook_ppb_write,
                         begin=self.PPB_REGION_BASE,
                         end=self.PPB_REGION_BASE + self.PPB_REGION_SIZE - 1)
        self.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED, self._hook_unmapped)
        self.uc.hook_add(UC_HOOK_MEM_WRITE_UNMAPPED, self._hook_unmapped)
        self.uc.hook_add(UC_HOOK_INTR, self._hook_intr)
        # 注意:不再注册 UC_HOOK_CODE(_hook_code 性能极差,每条指令回调 Python)
        # icount 通过 step(count=N) 的 N 累加估算

    # ---------- 固件加载 ----------
    def load_firmware(self, fw: FirmwareImage) -> None:
        self.firmware = fw
        # 写入 Flash 镜像
        self.uc.mem_write(FLASH_BASE, fw.data)
        # 设置初始 SP 与 PC
        self.uc.reg_write(UC_ARM_REG_SP, fw.initial_sp & 0xFFFFFFFF)
        # Cortex-M:PC 的 bit0 必须为 1 以表示 Thumb 状态
        self.uc.reg_write(UC_ARM_REG_PC, (fw.entry_point & 0xFFFFFFFE) | 1)
        # CPSR:Thumb(bit5)+ Supervisor 模式(bit0-4=0x13)
        self.uc.reg_write(UC_ARM_REG_CPSR, 0x00000023)
        # 设置 VTOR 指向 Flash 起始(在 PPB 中)
        vtor = FLASH_BASE
        self.ppb[SCB_VTOR - PPB_BASE:SCB_VTOR - PPB_BASE + 4] = vtor.to_bytes(4, "little")

        # 外设合理初值:GPIOB PB11(DC) 默认低=命令模式,GPIOC PC9(RST) 默认高=解除复位
        self.gpiob.write(self.gpiob.ODR, 4, 0)
        self.gpioc.write(self.gpioc.ODR, 4, (1 << 9))
        # 为了兼容"固件未发 0xAF 开屏命令"的情况,默认强制打开显示
        # (真正的 Flipper Zero 启动时 LCD 本来就是亮的,固件会在初始化序列里再写 0xAF)
        self.display.turn_on()

    # ---------- 按键 ----------
    def set_button(self, name: str, pressed: bool) -> None:
        if name not in BUTTON_MAP:
            return
        gpio_base, pin_bit = BUTTON_MAP[name]
        gpio = self.peripherals[gpio_base]
        gpio.set_button(pin_bit, pressed)
        # 产生 EXTI 下降沿中断(按键 IRQ 号 = pin 号,PB/PC 引脚号)
        # 这里只把 pending 置位,实际触发由主循环决定
        if pressed:
            pin_no = {1: 0, 2: 1, 4: 2, 8: 3, 0x10: 4, 0x20: 5}.get(pin_bit, pin_bit.bit_length() - 1)
            self._pend_irq(pin_no)

    # ---------- 主执行循环 ----------
    def step(self, n_instructions: int = 1000) -> None:
        """执行 N 条指令,期间检查 SysTick / pending IRQ。"""
        # emu_start 的 begin 必须带 Thumb 位(bit0=1),否则 unicorn 当 ARM 解码
        begin = self.uc.reg_read(UC_ARM_REG_PC) | 1
        try:
            self.uc.emu_start(begin, 0, timeout=0, count=n_instructions)
        except Exception as e:
            self.running = False
            raise
        # 累加指令计数(不再依赖 _hook_code)
        self.icount += n_instructions
        # 检查 SysTick 是否到期
        if (self.systick_ctrl & 0x1) and self.systick_load:
            self.systick_countdown -= n_instructions
            if self.systick_countdown <= 0:
                self.systick_countdown = self.systick_load
                self.systick_val = 0
                if self.systick_ctrl & 0x2:  # TICKINT
                    self._fire_exception(EXC_SYSTICK)
        # 检查 pending IRQ
        self._dispatch_pending_irq()

    # ---------- 异常机制 ----------
    def _vector_address(self, exception: int) -> int:
        vtor = int.from_bytes(self.ppb[SCB_VTOR - PPB_BASE:SCB_VTOR - PPB_BASE + 4], "little")
        tbl_off = exception * 4
        val = int.from_bytes(self.uc.mem_read(vtor + tbl_off, 4), "little")
        # 保留 Thumb 位(bit0=1),否则 unicorn 当 ARM 解码
        return val | 1

    def _fire_exception(self, exception: int) -> None:
        """手动进入异常:压栈 8 字,设置 LR=EXC_RETURN,PC=向量。"""
        if self.in_handler >= 4:
            return  # 嵌套过深,忽略
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        sp -= 32
        # 压入 {R0,R1,R2,R3,R12,LR,PC,xPSR}
        r0 = self.uc.reg_read(UC_ARM_REG_R0)
        r1 = self.uc.reg_read(UC_ARM_REG_R1)
        r2 = self.uc.reg_read(UC_ARM_REG_R2)
        r3 = self.uc.reg_read(UC_ARM_REG_R3)
        r12 = self.uc.reg_read(UC_ARM_REG_R12)
        lr = self.uc.reg_read(UC_ARM_REG_LR)
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        xpsr = self.uc.reg_read(UC_ARM_REG_XPSR)
        frame = struct.pack("<IIIIIIII", r0, r1, r2, r3, r12, lr, pc, xpsr)
        self.uc.mem_write(sp, frame)
        self.uc.reg_write(UC_ARM_REG_SP, sp)
        # EXC_RETURN:thread → handler,使用 MSP,无 FPU
        self.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFF9)
        handler_addr = self._vector_address(exception)
        self.uc.reg_write(UC_ARM_REG_PC, handler_addr)
        self.in_handler += 1
        if exception >= IRQ0_OFFSET:
            irq = exception - IRQ0_OFFSET
            self.nvic_active[irq // 32] |= 1 << (irq % 32)

    def _return_from_exception(self, exc_return: int) -> None:
        """从异常返回:出栈 8 字恢复上下文。"""
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        frame = self.uc.mem_read(sp, 32)
        r0, r1, r2, r3, r12, lr, pc, xpsr = struct.unpack("<IIIIIIII", frame)
        self.uc.reg_write(UC_ARM_REG_R0, r0)
        self.uc.reg_write(UC_ARM_REG_R1, r1)
        self.uc.reg_write(UC_ARM_REG_R2, r2)
        self.uc.reg_write(UC_ARM_REG_R3, r3)
        self.uc.reg_write(UC_ARM_REG_R12, r12)
        self.uc.reg_write(UC_ARM_REG_LR, lr)
        # Cortex-M:返回 PC 必须保留 Thumb 位(bit0=1),否则 unicorn 当 ARM 解码
        self.uc.reg_write(UC_ARM_REG_PC, pc | 1)
        self.uc.reg_write(UC_ARM_REG_XPSR, xpsr & 0xF8000000 | (1 << 24))
        self.uc.reg_write(UC_ARM_REG_SP, sp + 32)
        if self.in_handler > 0:
            self.in_handler -= 1

    # ---------- NVIC ----------
    def _pend_irq(self, irq: int) -> None:
        if 0 <= irq < 128:
            self.nvic_pending[irq // 32] |= 1 << (irq % 32)

    def _dispatch_pending_irq(self) -> None:
        if self.in_handler > 0:
            return  # 在 handler 中时不抢占(简化版,无优先级)
        for i in range(4):
            pending = self.nvic_pending[i] & self.nvic_iser[i]
            if pending:
                irq = i * 32 + (pending & -pending).bit_length() - 1
                self.nvic_pending[i] &= ~(1 << (irq % 32))
                self._fire_exception(IRQ0_OFFSET + irq)
                return

    # ---------- 钩子 ----------
    def _hook_mem_read(self, uc, access, address, size, value, user_data):
        per = self._find_peripheral(address)
        if per is not None:
            off = address - per.base
            val = per.read(off, size)
            # 把读出的值写回影子内存,让 unicorn 把它送回程序
            uc.mem_write(address & ~0x3, val.to_bytes(4, "little"))

    def _hook_mem_write(self, uc, access, address, size, value, user_data):
        per = self._find_peripheral(address)
        if per is not None:
            off = address - per.base
            per.write(off, size, value)
            # 写入影子内存(对齐到 4 字节),避免后续读回污染
            uc.mem_write(address & ~0x3, (value & ((1 << (8 * size)) - 1)).to_bytes(4, "little"))

    def _find_peripheral(self, address: int) -> Optional[Peripheral]:
        for base, per in self.peripherals.items():
            if per.base <= address < per.base + per.size:
                return per
        return None

    # ---------- PPB(NVIC/SysTick/SCB)----------
    def _hook_ppb_read(self, uc, access, address, size, value, user_data):
        off = address - PPB_BASE
        # SysTick
        if SYSTICK_BASE <= address < SYSTICK_BASE + 0x10:
            val = self._systick_read(address - SYSTICK_BASE, size)
            uc.mem_write(address & ~0x3, val.to_bytes(4, "little"))
            return
        # NVIC ISER/ISPR/ICPR 寄存器
        if NVIC_BASE <= address < NVIC_BASE + 0x300:
            val = self._nvic_read(address - NVIC_BASE, size)
            uc.mem_write(address & ~0x3, val.to_bytes(4, "little"))
            return
        # SCB VTOR / 其它
        val = int.from_bytes(self.ppb[off:off + size], "little")
        uc.mem_write(address & ~0x3, val.to_bytes(4, "little"))

    def _hook_ppb_write(self, uc, access, address, size, value, user_data):
        off = address - PPB_BASE
        if SYSTICK_BASE <= address < SYSTICK_BASE + 0x10:
            self._systick_write(address - SYSTICK_BASE, size, value)
            return
        if NVIC_BASE <= address < NVIC_BASE + 0x300:
            self._nvic_write(address - NVIC_BASE, size, value)
            return
        self.ppb[off:off + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")

    def _systick_read(self, off, size):
        if off == 0x00:   # CTRL
            return self.systick_ctrl
        if off == 0x04:   # LOAD
            return self.systick_load
        if off == 0x08:   # VAL
            return self.systick_val
        if off == 0x0C:   # CALIB
            return 0x00400000
        return 0

    def _systick_write(self, off, size, value):
        if off == 0x00:
            self.systick_ctrl = value & 0x7
            if value & 0x1 and self.systick_load:
                self.systick_countdown = self.systick_load
        elif off == 0x04:
            self.systick_load = value & 0xFFFFFF
        elif off == 0x08:
            self.systick_val = 0
            self.systick_countdown = self.systick_load if self.systick_ctrl & 0x1 else 0

    def _nvic_read(self, off, size):
        # ISER(offset 0x00)/ICER(0x80) 读出 enable
        # ISPR(0x100)/ICPR(0x180) 读出 pending
        # IABR(0x200) 读出 active
        idx = off // 4
        if off < 0x80:
            return self.nvic_iser[idx] if idx < 4 else 0
        if off < 0x100:
            return self.nvic_iser[(off - 0x80) // 4] if (off - 0x80) // 4 < 4 else 0
        if off < 0x180:
            return self.nvic_pending[(off - 0x100) // 4] if (off - 0x100) // 4 < 4 else 0
        if off < 0x200:
            return self.nvic_pending[(off - 0x180) // 4] if (off - 0x180) // 4 < 4 else 0
        if off < 0x300:
            return self.nvic_active[(off - 0x200) // 4] if (off - 0x200) // 4 < 4 else 0
        return 0

    def _nvic_write(self, off, size, value):
        # ISER:置位 enable
        if off < 0x80:
            idx = off // 4
            if idx < 4:
                self.nvic_iser[idx] |= value
        # ICER:清 enable
        elif off < 0x100:
            idx = (off - 0x80) // 4
            if idx < 4:
                self.nvic_iser[idx] &= ~value
        # ISPR:置 pending
        elif off < 0x180:
            idx = (off - 0x100) // 4
            if idx < 4:
                self.nvic_pending[idx] |= value
        # ICPR:清 pending
        elif off < 0x200:
            idx = (off - 0x180) // 4
            if idx < 4:
                self.nvic_pending[idx] &= ~value
                self.nvic_active[idx] &= ~value  # 也清 active(简化)

    # ---------- 未映射内存 ----------
    def _hook_unmapped(self, uc, access, address, size, value, user_data):
        # 对所有未映射访问,登记但不崩溃:返回 0 / 忽略写
        try:
            uc.mem_map(address & ~0xFFF, 0x1000, UC_PROT_ALL)
            if access == 2:  # WRITE
                uc.mem_write(address, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))
            return True
        except Exception:
            return False

    # ---------- 中断钩子(主要处理 EXC_RETURN)----------
    def _hook_intr(self, uc, intno, user_data):
        lr = uc.reg_read(UC_ARM_REG_LR)
        # LR 是 EXC_RETURN 值 → 当前是从异常返回(我们的手动 handler 里 BX LR 触发)
        if lr & 0xFF000000 == 0xFF000000:
            self._return_from_exception(lr)
            return
        # unicorn 内置 HardFault(intno=2/3):通常因为非法内存访问或指令错误
        # 这里只记录,不再递归 fire(避免无限 HardFault 循环)
        # 真实固件遇到这种情况说明外设仿真有缺漏,交给上层看 UART 输出排查
        return
