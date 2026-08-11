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
    UC_HOOK_MEM_FETCH_UNMAPPED,
    UC_HOOK_INTR, UC_ERR_OK, UC_PROT_ALL,
    UC_MEM_WRITE_UNMAPPED, UC_MEM_READ_UNMAPPED,
)
from unicorn import UcError
from unicorn.arm_const import (
    UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8,
    UC_ARM_REG_R12, UC_ARM_REG_CPSR, UC_ARM_REG_XPSR,
    UC_ARM_REG_MSP, UC_ARM_REG_PSP, UC_ARM_REG_BASEPRI, UC_ARM_REG_PRIMASK,
)

from .stm32wb55 import (
    FLASH_BASE, FLASH_SIZE, SRAM1_BASE, SRAM1_SIZE, SRAM2_BASE, SRAM2_SIZE,
    SYSTEM_MEM_BASE, SYSTEM_MEM_SIZE, PERIPH_BASE, PPB_BASE,
    RCC_BASE, PWR_BASE, FLASH_REG_BASE, RNG_BASE, EXTI_BASE,
    DMA1_BASE, DMAMUX1_BASE, CRC_BASE, GPIOA_BASE, GPIOB_BASE, GPIOC_BASE,
    GPIOD_BASE, GPIOH_BASE, ADC_BASE, SPI2_BASE, USART1_BASE, USART2_BASE,
    TIM2_BASE, I2C1_BASE, AES1_BASE, AES2_BASE,
    IPCC_BASE, HWSEM_BASE, PKA_BASE, SAES_BASE, RTC_BASE,
    SYSTICK_BASE, SCB_BASE, NVIC_BASE, SCB_VTOR, SCB_CCR, SCB_SHPR,
    BUTTON_MAP,
    SYSCFG_BASE, COMP_BASE, TIM1_BASE, TIM16_BASE, TIM17_BASE,
    LPUART1_BASE, LPTIM1_BASE, LPTIM2_BASE, TSC_BASE, VREFBUF_BASE,
)
from .display import ST7567
from .firmware_loader import FirmwareImage


# Cortex-M 系统异常号
EXC_RESET = 1
EXC_SVCALL = 11
EXC_PENDSV = 14
EXC_SYSTICK = 15
IRQ0_OFFSET = 16  # IRQ0 -> exception 16

# SCB 寄存器偏移
SCB_ICSR = 0xE000ED04  # Interrupt Control and State Register
SCB_ICSR_PENDSVSET = 1 << 28
SCB_ICSR_PENDSTSET = 1 << 26

# DWT (Data Watchpoint and Trace) — 0xE0001000
DWT_BASE      = 0xE0001000
DWT_CTRL      = DWT_BASE + 0x00  # Control: bit0=CYCCNTENA, bits[28:31]=NUMCOMP
DWT_CYCCNT    = DWT_BASE + 0x04  # Cycle counter (increment per cycle)
DWT_CPICNT    = DWT_BASE + 0x08
DWT_EXCCNT    = DWT_BASE + 0x0C
DWT_SLEEPCNT  = DWT_BASE + 0x10
DWT_LSUCNT    = DWT_BASE + 0x14
DWT_FOLDCNT   = DWT_BASE + 0x18
DWT_PCSR      = DWT_BASE + 0x1C  # Program Counter Sample

# DEMCR (Debug Exception and Monitor Control Register) — 0xE000EDFC
DEMCR         = 0xE000EDFC
DEMCR_TRCENA  = 1 << 24  # Enable DWT/ITM trace

# Unicorn ARM 异常号
UC_ARM_EXCP_SWI = 2  # SVC/SWI 异常


# ====== 通用外设基类 ======
class Peripheral:
    """简单寄存器透传外设:大部分寄存器读返回写入值,可被子类覆盖。"""
    def __init__(self, base: int, size: int, name: str = ""):
        self.base = base
        self.size = size
        self.name = name or self.__class__.__name__
        self.regs = bytearray(size)

    def read(self, offset: int, size: int) -> int:
        # 防御:offset 越界时返回 0,避免切片异常
        if offset < 0 or offset + size > len(self.regs):
            return 0
        return int.from_bytes(self.regs[offset:offset + size], "little")

    def write(self, offset: int, size: int, value: int) -> None:
        # 防御:offset 越界时静默丢弃,避免切片异常
        if offset < 0 or offset + size > len(self.regs):
            return
        # 关键:value 可能 >32 位 (Unicorn 在 Windows 上可能传递 64 位值)
        # 必须先掩码到 size 对应的位数,否则 to_bytes 会抛 OverflowError
        mask = (1 << (8 * size)) - 1
        self.regs[offset:offset + size] = (value & mask).to_bytes(size, "little")


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
        # 防御:value 可能 >32 位 (Unicorn Windows bug),先掩码
        value &= 0xFFFFFFFF
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


class RCC(Peripheral):
    """STM32WB55 RCC (Reset and Clock Control) 仿真。

    关键:所有时钟就绪标志位返回 1,否则固件会死循环等待时钟就绪。
    参考:RM0434 STM32WB55 参考手册
    """
    CR      = 0x00   # Clock control register
    ICSCR   = 0x04   # Internal clock sources calibration
    CFGR    = 0x08   # Clock configuration register
    CRRCR   = 0x0C   # Clock recovery RC register (HSI48)
    EXTCFGR = 0x10   # Extended clock configuration
    BDCR    = 0x90   # Backup domain control register (LSE)
    CSR     = 0x94   # Clock control & status register (LSI)
    # CR 位定义 (STM32WB55 RM0434)
    # 注意:STM32WB55 的 RCC_CR 位布局与 STM32F1/F4 不同!
    #   HSEON=bit8, HSECSSON=bit9, HSEBYP=bit16, HSERDY=bit17
    HSION      = 1 << 0     # HSI enable
    HSIRDY     = 1 << 1     # HSI ready
    HSIKERON   = 1 << 4     # HSI kernel enable
    HSIKERDY   = 1 << 7     # HSI kernel ready
    HSEON      = 1 << 8     # HSE enable
    HSECSSON   = 1 << 9     # HSE clock security system enable
    HSE_CSS_RDY = 1 << 10   # 固件在 HSERDY 后轮询此位(可能是 CSS/HSE2 状态)
    HSEBYP     = 1 << 16    # HSE bypass
    HSERDY     = 1 << 17    # HSE ready (Flipper uses external crystal)
    PLLON      = 1 << 24    # PLL enable
    PLLRDY     = 1 << 25    # PLL ready
    PLLSAI1ON  = 1 << 26    # PLLSAI1 enable
    PLLSAI1RDY = 1 << 27    # PLLSAI1 ready (48MHz/ADC clock)
    # MSI (firmware disables MSI then checks MSIRDY==0)
    MSIRDY     = 1 << 2     # MSI ready (RM0434: RCC_CR bit 2, 不同位置!)
    # BDCR 位定义
    LSERDY   = 1 << 1    # LSE ready (32.768kHz external crystal for RTC)
    # CSR 位定义
    LSI1RDY  = 1 << 1    # LSI1 ready (RM0434: RCC_CSR bit 1)
    # CRRCR 位定义
    HSI48RDY = 1 << 1    # HSI48 ready (USB/RNG clock)
    # CFGR SWS 位
    SWS_PLL  = 0b10 << 2  # System clock switch status: PLL (RM0434: SWS=0b10 for PLL, 0b11 is reserved!)

    def __init__(self, base, size, name="RCC"):
        super().__init__(base, size, name)
        # 复位值:HSION=1 (HSI 默认开启), HSIRDY=1
        # RM0434: RCC_CR reset value = 0x00000061 (HSION | HSIRDY | HSIASFS)
        cr_reset = self.HSION | self.HSIRDY | (1 << 3)  # HSIASFS
        self.regs[0:4] = cr_reset.to_bytes(4, "little")

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        # CR: 时钟就绪标志位跟随使能位
        # 固件会关闭 HSI 后轮询 HSIRDY==0,所以不能无条件强制为 1
        if offset == self.CR or (offset <= self.CR + 4 and offset + size > self.CR):
            cr = int.from_bytes(self.regs[0:4], "little")
            # 每个时钟:使能位为 1 时,就绪位也为 1 (硬件就绪)
            if cr & self.HSION:
                cr |= self.HSIRDY
            else:
                cr &= ~self.HSIRDY
            if cr & self.HSIKERON:
                cr |= self.HSIKERDY
            if cr & self.HSEON:
                cr |= self.HSERDY
            else:
                cr &= ~self.HSERDY
            if cr & self.PLLON:
                cr |= self.PLLRDY
            if cr & self.PLLSAI1ON:
                cr |= self.PLLSAI1RDY
            # HSEBYP 和 HSE_CSS_RDY 始终保持 (固件已配置)
            # HSE_CSS_RDY 在 HSE 启用时跟随
            if cr & self.HSEON:
                cr |= self.HSE_CSS_RDY
            val = cr
            if size == 4:
                return val
            elif size == 2:
                return val & 0xFFFF
            else:
                return val & 0xFF
        # CRRCR: HSI48 ready (STM32WB55 可能在 0x0C 或 0x98 访问)
        if offset == self.CRRCR or offset == 0x98:
            return val | self.HSI48RDY
        # BDCR: LSE ready (固件等 LL_RCC_LSE_IsReady())
        if offset == self.BDCR:
            val = int.from_bytes(self.regs[self.BDCR:self.BDCR + 4], "little")
            val |= self.LSERDY
            return val & 0xFFFFFFFF
        # CSR: LSI1 ready (固件等 LL_RCC_LSI1_IsReady())
        if offset == self.CSR:
            val = int.from_bytes(self.regs[self.CSR:self.CSR + 4], "little")
            val |= self.LSI1RDY
            return val & 0xFFFFFFFF
        # CFGR: SWS 跟随 SW,但 SW=0b11 时映射为 0b10 (PLL)。
        #   固件写 SW=0b11 并轮询 SWS 等待 == 0b11。
        #   但 SWS=0b11 会导致 PendSV 上下文切换异常(任务栈损坏)。
        #   策略:SW=0b11 时返回 SWS=0b10,固件检测到 SWS!=0b11 会
        #   跳过等待继续执行(部分固件版本接受 SWS=PLL 作为已切换)。
        #   如果固件严格要求 SWS==0b11,会进入超时循环但不会崩溃。
        if offset == self.CFGR:
            val = int.from_bytes(self.regs[self.CFGR:self.CFGR + 4], "little")
            sw = val & 0x03           # SW = bits[1:0]
            if sw == 0b11:            # reserved → 当作 PLL
                sw = 0b10
            val &= ~(0x03 << 2)       # 清 SWS
            val |= (sw << 2)          # SWS = SW (切换完成)
            if size == 4:
                return val
            elif size == 2:
                return val & 0xFFFF
            else:
                return val & 0xFF
        return val


class FLASHController(Peripheral):
    """STM32WB55 FLASH 控制寄存器仿真。"""
    ACR = 0x00  # Access control register

    def read(self, offset, size):
        if offset == self.ACR:
            # 返回合理的 latency: Flash 已就绪
            val = int.from_bytes(self.regs[0:4], "little")
            val |= (1 << 0)  # Latency bits (1 wait state)
            return val & 0xFFFFFFFF
        return super().read(offset, size)


class PWR(Peripheral):
    """STM32WB55 PWR (Power Control) 仿真。

    RM0434 寄存器映射:
      CR1=0x00, CR2=0x04, CR3=0x08, CR4=0x0C, CR5=0x10
      SR1=0x94, SR2=0x98 (注意:SR1/SR2 在 CR 系列之后,不是 0x00/0x04!)
    """
    CR1 = 0x00
    CR2 = 0x04
    CR3 = 0x08
    CR4 = 0x0C
    CR5 = 0x10
    SR1 = 0x94
    SR2 = 0x98

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        # SR1: 所有 wakeup 标志位清零 (ready)
        if offset == self.SR1:
            return val & ~0x1F
        # SR2: REGLPS=1 (regulator in main mode), REGF=0 (no fallback)
        if offset == self.SR2:
            return val | (1 << 13)
        # CR4: C2BOOT 位读为 1(CPU2 已启动)
        if offset == self.CR4:
            return val | (1 << 15)
        return val


class IPCC(Peripheral):
    """STM32WB55 IPCC (Inter-Processor Communication Controller) 仿真。

    Flipper Zero 固件在启动 CPU2 (Radio) 后,会通过 IPCC 寄存器等待 CPU2 应答。
    由于 CPU2 未仿真,当 CPU1 通过 CPU1TOC2SR 发送消息时,自动模拟 CPU2 的响应:
    - 清除 CPU1TOC2SR 对应位 (CPU2 已接收)
    - 设置 CPU2TOC1SR 对应位 (CPU2 已回复)
    - 如果该通道中断未屏蔽,pend IPCC IRQ
    """
    CPU1CR = 0x00
    CPU1MR = 0x04
    CPU1SCR = 0x08
    CPU1TOC2SR = 0x0C   # CPU1 To CPU2 Status Register
    CPU1TOC2MR = 0x10
    CPU2CR = 0x20
    CPU2MR = 0x24
    CPU2SCR = 0x28
    CPU2TOC1SR = 0x2C   # CPU2 To CPU1 Status Register
    CPU2TOC1MR = 0x30

    def __init__(self, base, size, name="IPCC"):
        super().__init__(base, size, name)
        self._vm = None
        self._irq = None  # IPCC RX IRQ number (set by FlipperVM)

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        return val & 0xFFFFFFFF

    def write(self, offset, size, value):
        mask = (1 << (8 * size)) - 1
        value &= mask

        # CPU1 向 CPU2 发送消息:写 CPU1TOC2SR 设置 bit = 通知 CPU2
        if offset == self.CPU1TOC2SR:
            old = int.from_bytes(self.regs[self.CPU1TOC2SR:self.CPU1TOC2SR + 4], "little")
            new_channels = value & ~old  # 新设置的通道
            if new_channels:
                # 清除 CPU1->CPU2 状态 (CPU2 已接收)
                self.regs[self.CPU1TOC2SR:self.CPU1TOC2SR + 4] = (old & ~value).to_bytes(4, "little")
                # 设置 CPU2->CPU1 状态 (CPU2 已回复)
                c2toc1 = int.from_bytes(self.regs[self.CPU2TOC1SR:self.CPU2TOC1SR + 4], "little")
                c2toc1 |= new_channels
                self.regs[self.CPU2TOC1SR:self.CPU2TOC1SR + 4] = c2toc1.to_bytes(4, "little")
                # 检查中断屏蔽:CPU2TOC1MR 中 0 = 未屏蔽(允许中断)
                c2mr = int.from_bytes(self.regs[self.CPU2TOC1MR:self.CPU2TOC1MR + 4], "little")
                unmasked = new_channels & ~c2mr
                if unmasked and self._vm is not None and self._irq is not None:
                    self._vm._pend_irq(self._irq)
            return

        # CPU1 清除 CPU2->CPU1 状态 (确认收到 CPU2 回复):写 CPU2TOC1SR 清 bit
        if offset == self.CPU2TOC1SR:
            old = int.from_bytes(self.regs[self.CPU2TOC1SR:self.CPU2TOC1SR + 4], "little")
            self.regs[self.CPU2TOC1SR:self.CPU2TOC1SR + 4] = (old & ~value).to_bytes(4, "little")
            return

        # 其他寄存器直接写
        self.regs[offset:offset + size] = value.to_bytes(size, "little")


class HWSEM(Peripheral):
    """STM32WB55 Hardware Semaphores (32 semaphores) 仿真。

    R = 0x00000000 = 所有信号量空闲,任何读都得到"已被我持有"的状态(读时写入持有者 ID)。
    这里简化成:任何信号量读立即释放(空闲),所以写锁定/读解锁都瞬间成功。
    """
    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        # 每个 R 寄存器: bit31=1 表示空闲,其余 = Core ID(0=CPU1)。
        # 固件流程:读 R 寄存器 -> 如果 bit31==1 表示我拿到了。
        # 为了让固件立刻拿到信号量,返回 0x80000001 (Core ID=CPU1, lock acquired)。
        return 0x80000001 & ((1 << (8 * size)) - 1)


class RNG(Peripheral):
    """STM32WB55 Random Number Generator 仿真。

    RNG_CR 写入后读 RNG_SR = 0x1 (DRDY=1), DR 返回确定性伪随机数(避免卡死)。
    """
    CR = 0x00
    SR = 0x04
    DR = 0x08

    def __init__(self, base, size, name="RNG"):
        super().__init__(base, size, name)
        self._counter = 0x12345678

    def read(self, offset, size):
        if offset == self.SR:
            # DRDY=1 (data ready), 无错误
            return 0x00000001
        if offset == self.DR:
            # 伪随机序列(足够骗过固件启动阶段的熵收集)
            self._counter = ((self._counter * 1664525) + 1013904223) & 0xFFFFFFFF
            return self._counter
        return int.from_bytes(self.regs[offset:offset + size], "little")


class PKA(Peripheral):
    """STM32WB55 PKA (Public Key Accelerator) 仿真。返回 PKA busy=0。"""
    SR = 0x08

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        if offset == self.SR:
            return 0  # PROCEND=1? 实际上 PROCEND bit0=1 表示处理完毕
        return val

    def write(self, offset, size, value):
        super().write(offset, size, value)
        # 任何写触发"立刻完成":设置 SR PROCEND=1
        # 但我们读 SR 返回 0, 对简化场景足够


class RTC(Peripheral):
    """STM32WB55 RTC 仿真。初始化完成标志 + 静态时间。"""
    TR = 0x00
    DR = 0x04
    SSR = 0x08
    ICSR = 0x0C
    PRER = 0x10
    WUTR = 0x14
    CR = 0x18
    DR_BAKP = 0x50  # 备份寄存器起点, 32 * 4B

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        if offset == self.ICSR:
            # INITF=1(已进入 init mode), RSF=1 (registers synced), INITS=1
            return 0x00000007
        if offset == self.CR:
            # RTCALRM = 0, WUTE = 0, WUTIE = 0, ALRAIE = 0, TSE = 0, ...
            # 返回默认,不特别设置
            return val
        if offset == self.DR:
            # 固定日期:2026-08-10 Monday
            return (0x26 << 16) | (0x08 << 8) | (0x01 << 5) | 0x10
        if offset == self.TR:
            # 固定时间:12:00:00
            return (0x12 << 16) | (0x00 << 8) | 0x00
        return val


class I2C1(Peripheral):
    """STM32WB55 I2C1 仿真。

    Flipper Zero 通过 I2C1 连接:
      - BQ27220 电池电量计
      - 触摸/传感器等

    仿真策略:让所有 I2C 事务立即"成功"完成。
    ISR 始终返回 TXIS=1 | RXNE=1 | TC=1 | STOPF=1,使固件的轮询循环立即退出。
    """
    CR1     = 0x00
    CR2     = 0x04
    OAR1    = 0x08
    OAR2    = 0x0C
    TIMINGR = 0x10
    TIMEOUTR = 0x14
    ISR     = 0x18
    ICR     = 0x1C
    PECR    = 0x20
    RXDR    = 0x24
    TXDR    = 0x28

    # ISR 位定义
    ISR_TXE   = 1 << 0    # TXDR empty
    ISR_TXIS  = 1 << 1    # TX interrupt status (ready to send)
    ISR_RXNE  = 1 << 2    # RXDR not empty
    ISR_ADDR  = 1 << 3    # Address matched
    ISR_NACKF = 1 << 4    # NACK received
    ISR_STOPF = 1 << 5    # Stop detected
    ISR_TC    = 1 << 6    # Transfer complete
    ISR_TCR   = 1 << 7    # Transfer complete reload
    ISR_BUSY  = 1 << 15   # Bus busy

    def __init__(self, base, name="I2C1"):
        super().__init__(base, 0x40, name)
        # ISR: 事务已完成,总线空闲,NACK=0
        self._isr = (self.ISR_TXE | self.ISR_TXIS | self.ISR_RXNE |
                     self.ISR_TC | self.ISR_TCR | self.ISR_STOPF)

    def read(self, offset, size):
        if offset == self.ISR:
            return self._isr & ((1 << (8 * size)) - 1)
        if offset == self.RXDR:
            # 读 RXDR 返回 0xFF (I2C 总线默认上拉电平)
            return 0xFF & ((1 << (8 * size)) - 1)
        return super().read(offset, size)

    def write(self, offset, size, value):
        if offset == self.ICR:
            # 写 ICR 清除标志 — 保持所有成功标志不变
            return
        if offset == self.TXDR:
            # 写 TXDR — 数据已"发送"
            return
        super().write(offset, size, value)


class SYSCFG(Peripheral):
    """STM32WB55 SYSCFG (System Configuration Controller) 仿真。

    主要用于 EXTI 线映射 (EXTICR) 和其他系统配置。
    寄存器透传即可。
    """
    def __init__(self, base, name="SYSCFG"):
        super().__init__(base, 0x400, name)


class LPTIM(Peripheral):
    """STM32WB55 LPTIM (Low-Power Timer) 仿真。

    真实推进 CNT: 每调用 advance(N) 就让 CNT += N,到达 ARR 后置 ARRM。
    这样固件 LPTIM 睡眠不会卡死。
    """
    ISR  = 0x00   # Interrupt and Status: ARRM=bit4 ARROK=bit5 UP=bit6 DOWN=bit7
    IER  = 0x04   # Interrupt Enable: ARRIE=bit4
    CFGR = 0x08
    CR   = 0x0C   # Control: ENABLE=bit0 SNGSTRT=bit2 CNTSTRT=bit1
    CMP  = 0x10
    ARR  = 0x14
    CNT  = 0x18

    def __init__(self, base, size, name="LPTIM"):
        super().__init__(base, size, name)
        self._cnt = 0
        self._arr = 0x0000FFFF
        # IRQ 号: STM32WB55 RM0434 中 LPTIM1=IRQ31, LPTIM2=IRQ32.
        # 若名字里有 "2" 则 +1
        self._irq = 31 + (1 if "2" in name else 0)
        self._vm = None  # 由 FlipperVM 注入,用于触发 NVIC

    def read(self, offset, size):
        if offset == self.CNT:
            # 把当前 CNT 同步回 regs 供读
            self._cnt &= 0xFFFF
            self.regs[self.CNT:self.CNT + 4] = self._cnt.to_bytes(4, "little")
            return self._cnt
        if offset == self.ARR:
            return self._arr
        return super().read(offset, size)

    def write(self, offset, size, value):
        super().write(offset, size, value)
        if offset == self.ARR:
            self._arr = value & 0xFFFF
            if self._arr == 0:
                self._arr = 1  # 防止除以 0
        # 只要 IER 开启了任何中断,同时兜底把 NVIC ISER 对应位打开,
        # 保证 WFI 等中断睡眠能被唤醒(即使固件漏开 NVIC)
        if offset == self.IER and self._vm is not None:
            if value & 0x3F:
                iser_idx = self._irq // 32
                iser_bit = 1 << (self._irq % 32)
                if iser_idx < len(self._vm.nvic_iser):
                    self._vm.nvic_iser[iser_idx] |= iser_bit

    def advance(self, cycles: int) -> bool:
        """让定时器前进一步,返回是否产生了 ARRM 事件。"""
        cr = int.from_bytes(self.regs[self.CR:self.CR + 4], "little")
        if not (cr & 0x01):  # ENABLE == 0
            return False
        # CNTSTRT/SNGSTRT 启动了就递增
        self._cnt = (self._cnt + cycles) & 0xFFFFFFFF
        arrm = False
        while self._cnt >= self._arr:
            self._cnt -= self._arr
            arrm = True
        if arrm:
            # ISR.ARRM = bit4
            isr = int.from_bytes(self.regs[self.ISR:self.ISR + 4], "little")
            self.regs[self.ISR:self.ISR + 4] = (isr | (1 << 4)).to_bytes(4, "little")
            # 如果 IER.ARRIE=1,且有 VM 引用,触发 NVIC
            ier = int.from_bytes(self.regs[self.IER:self.IER + 4], "little")
            if (ier & (1 << 4)) and self._vm is not None:
                self._vm._pend_irq(self._irq)
            return True
        return False


class LPUART(Peripheral):
    """STM32WB55 LPUART1 仿真。用于低功耗串口。
    ISR: TXE=1, TC=1 (发送就绪)
    """
    ISR = 0x1C
    TDR = 0x28
    RDR = 0x24

    def __init__(self, base, name="LPUART1", on_tx=None):
        super().__init__(base, 0x400, name)
        self.on_tx = on_tx
        self._isr = (1 << 7) | (1 << 6)  # TXE | TC
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


class DMA1(Peripheral):
    """STM32WB55 DMA1 控制器仿真。

    支持 memory-to-peripheral 传输(用于 SPI2 TX 发送 LCD 数据)。
    当通道使能时,立即执行传输:从内存读取数据,写入目标外设寄存器。
    """
    # DMA1 寄存器布局 (每个通道 0x20 字节)
    ISR   = 0x00  # Interrupt Status (低16位: 4位/通道 x 7通道)
    IFCR  = 0x04  # Interrupt Flag Clear
    # 通道寄存器 (偏移 = ch * 0x20):
    CCR   = 0x00  # Control: EN=bit0, TCIE=bit1, MINC=bit4, DIR=bit5(0=M→P)
    CNDTR = 0x04  # Count
    CPAR  = 0x08  # Peripheral address
    CMAR  = 0x0C  # Memory address

    def __init__(self, base, size, name="DMA1"):
        super().__init__(base, size, name)
        self._vm = None

    def write(self, offset, size, value):
        mask = (1 << (8 * size)) - 1
        value &= mask
        self.regs[offset:offset + size] = value.to_bytes(size, "little")

        # 检测通道使能: CCR 的 EN 位 (bit0) 被设置
        # 通道 0 的 CCR 在 offset 0x08+0*0x20 = 0x08
        # 通道 1 的 CCR 在 offset 0x08+1*0x20 = 0x28
        # 通道 N 的 CCR 在 offset 0x08+N*0x20
        for ch in range(7):
            ccr_off = 0x08 + ch * 0x20
            if offset == ccr_off and (value & 1):
                self._execute_channel(ch)

    def _execute_channel(self, ch):
        """立即执行 DMA 通道传输。"""
        base = 0x08 + ch * 0x20
        ccr = int.from_bytes(self.regs[base:self.CCR + 4 - 0x08 + base], "little") if False else \
              int.from_bytes(self.regs[base:base + 4], "little")
        cndtr = int.from_bytes(self.regs[base + 4:base + 8], "little")
        cpar = int.from_bytes(self.regs[base + 8:base + 12], "little")
        cmar = int.from_bytes(self.regs[base + 12:base + 16], "little")

        if cndtr == 0 or cpar == 0:
            # 禁用通道
            self.regs[base:base + 4] = (ccr & ~1).to_bytes(4, "little")
            return

        # DIR: 0 = read from memory, write to peripheral
        # DIR: 1 = read from peripheral, write to memory
        dir_mem_to_periph = not (ccr & (1 << 5))
        minc = ccr & (1 << 4)  # Memory increment
        psize = (ccr >> 8) & 0x3  # Peripheral size: 0=8bit, 1=16bit, 2=32bit
        msize = (ccr >> 10) & 0x3  # Memory size

        byte_size = 1 << psize if dir_mem_to_periph else 1 << msize

        try:
            if dir_mem_to_periph:
                # Memory → Peripheral: 读取内存数据,写入外设
                for i in range(cndtr):
                    addr = cmar + (i * (1 << msize) if minc else 0)
                    data = int.from_bytes(
                        bytes(self._vm.uc.mem_read(addr, 1 << msize)), "little")
                    # 写入外设:通过 VM 的内存写钩子
                    self._vm.uc.mem_write(cpar, data.to_bytes(1 << psize, "little"))
            else:
                # Peripheral → Memory: 读取外设,写入内存
                for i in range(cndtr):
                    data = int.from_bytes(
                        bytes(self._vm.uc.mem_read(cpar, 1 << psize)), "little")
                    addr = cmar + (i * (1 << msize) if minc else 0)
                    self._vm.uc.mem_write(addr, data.to_bytes(1 << msize, "little"))
        except Exception:
            pass

        # 传输完成:清除 EN,设置 TCIF (Transfer Complete Interrupt Flag)
        self.regs[base:base + 4] = (ccr & ~1).to_bytes(4, "little")
        # ISR: 每通道 4 位,TCIF = bit1 (在通道的 4 位组内)
        isr = int.from_bytes(self.regs[0:4], "little")
        tcif_bit = 1 << (ch * 4 + 1)  # TCIF = GIF bit1
        isr |= tcif_bit
        self.regs[0:4] = isr.to_bytes(4, "little")

        # 如果 TCIE 使能,pend DMA 中断
        if ccr & (1 << 1) and self._vm is not None:
            # DMA1 通道中断号: ch 0=11, ch1=12, ..., ch6=17
            irq = 11 + ch
            self._vm._pend_irq(irq)


class TSC(Peripheral):
    """STM32WB55 TSC (Touch Sensing Controller) 仿真。返回 0 (无触摸)。"""
    def __init__(self, base, name="TSC"):
        super().__init__(base, 0x400, name)


class VREFBUF(Peripheral):
    """STM32WB55 VREFBUF (Voltage Reference Buffer) 仿真。CSR=就绪。"""
    CSR = 0x00

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        if offset == self.CSR:
            val |= (1 << 3) | (1 << 1)  # VREFEN | ENVR
        return val


class COMP(Peripheral):
    """STM32WB55 COMP (Comparator) 仿真。透传。"""
    def __init__(self, base, name="COMP"):
        super().__init__(base, 0x20, name)


class TIM(Peripheral):
    """通用定时器仿真。SR=0 (无事件)。"""
    SR = 0x10

    def read(self, offset, size):
        val = int.from_bytes(self.regs[offset:offset + size], "little")
        if offset == self.SR:
            return 0  # 无中断挂起
        return val


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

        # 内存映射 (真实 STM32WB55 行为)
        # Cortex-M: 地址 0x00000000 是 Flash 的别名 (boot from Flash)
        # 真实硬件:读地址 0 = 读 Flash 向量表 (SP / Reset_Handler 等)
        # load_firmware() 会把 Flash 数据同步写入地址 0 区域
        self.uc.mem_map(0x00000000, FLASH_SIZE, UC_PROT_ALL)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE, UC_PROT_ALL)
        self.uc.mem_map(SRAM1_BASE, SRAM1_SIZE + SRAM2_SIZE, UC_PROT_ALL)
        # SRAM 不显式填充:Unicorn 默认为 0x00。
        # 真实 STM32 上电后 SRAM 内容未定义,但 crt0 启动代码会清零 BSS 段。
        # 固件不应读取未初始化的堆内存;若读取,返回 0 (NULL) 是合理行为。
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
        self.rcc = RCC(RCC_BASE, 0x600, "RCC")
        self.pwr = PWR(PWR_BASE, 0x100, "PWR")
        self.flash_reg = FLASHController(FLASH_REG_BASE, 0x200, "FLASH")
        self.rng = RNG(RNG_BASE, 0x100, "RNG")
        self.dma1 = DMA1(DMA1_BASE, 0x400, "DMA1")
        self.dmamux = Peripheral(DMAMUX1_BASE, 0x100, "DMAMUX1")
        self.crc = Peripheral(CRC_BASE, 0x40, "CRC")
        self.adc = Peripheral(ADC_BASE, 0x400, "ADC")
        self.aes1 = Peripheral(AES1_BASE, 0x100, "AES1")
        self.aes2 = Peripheral(AES2_BASE, 0x100, "AES2")
        self.tim2 = TIM(TIM2_BASE, 0x400, "TIM2")
        self.tim1 = TIM(TIM1_BASE, 0x400, "TIM1")
        self.tim16 = TIM(TIM16_BASE, 0x400, "TIM16")
        self.tim17 = TIM(TIM17_BASE, 0x400, "TIM17")
        self.i2c1 = I2C1(I2C1_BASE, "I2C1")
        # 双核通信外设:CPU1 <-> CPU2 (Radio IPCC + 硬件信号量 + PKA + RTC)
        self.ipcc = IPCC(IPCC_BASE, 0x100, "IPCC")
        self.hwsem = HWSEM(HWSEM_BASE, 0x100, "HWSEM")
        self.pka = PKA(PKA_BASE, 0x800, "PKA")
        self.saes = Peripheral(SAES_BASE, 0x100, "SAES")
        self.rtc = RTC(RTC_BASE, 0x1000, "RTC")
        # 新增外设
        self.syscfg = SYSCFG(SYSCFG_BASE, "SYSCFG")
        self.comp = COMP(COMP_BASE, "COMP")
        self.lpuart1 = LPUART(LPUART1_BASE, "LPUART1", on_uart_tx)
        self.lptim1 = LPTIM(LPTIM1_BASE, 0x100, "LPTIM1")
        self.lptim2 = LPTIM(LPTIM2_BASE, 0x100, "LPTIM2")
        # LPTIM 需要反向引用 VM 以便触发 NVIC 中断
        self.lptim1._vm = self
        self.lptim2._vm = self
        # IPCC 也需要反向引用 VM 以便触发 NVIC 中断
        # STM32WB55: IPCC_C1_RX_IRQn = 35, IPCC_C1_TX_IRQn = 34
        self.ipcc._vm = self
        self.ipcc._irq = 35  # IPCC_C1_RX_IRQn
        # DMA1 需要反向引用 VM 以便执行内存→外设传输和触发中断
        self.dma1._vm = self
        self.tsc = TSC(TSC_BASE, "TSC")
        self.vrefbuf = VREFBUF(VREFBUF_BASE, 0x40, "VREFBUF")

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
            TIM2_BASE: self.tim2, TIM1_BASE: self.tim1,
            TIM16_BASE: self.tim16, TIM17_BASE: self.tim17,
            I2C1_BASE: self.i2c1,
            IPCC_BASE: self.ipcc, HWSEM_BASE: self.hwsem, PKA_BASE: self.pka,
            SAES_BASE: self.saes, RTC_BASE: self.rtc,
            SYSCFG_BASE: self.syscfg, COMP_BASE: self.comp,
            LPUART1_BASE: self.lpuart1,
            LPTIM1_BASE: self.lptim1, LPTIM2_BASE: self.lptim2,
            TSC_BASE: self.tsc, VREFBUF_BASE: self.vrefbuf,
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
        self._systick_countflag = False  # COUNTFLAG (bit 16 of CTRL)
        self.in_handler = 0   # 当前处于异常处理层数
        self._exc_return_psp = False  # 异常返回时是否使用 PSP
        self._exc_frame_stack = []  # 异常帧指针栈:每层 handler 进入时记录帧 SP
        self._handler_instr_count = 0  # handler 内执行的指令计数(用于超时检测)
        self._pendsv_pending = False  # PendSV pending (FreeRTOS 上下文切换)
        self._systick_pending = False  # SysTick pending (SCB_ICSR.PENDSTSET)
        self._emu_stopped_early = False  # emu_start 被 emu_stop 提前终止(异常返回)
        self._skip_current_instr = False  # 跳过当前指令(64位地址访问后)
        self._warn_64bit_count = 0       # 64位地址警告计数(限流)
        self._reg_truncate_counter = 0   # 寄存器截断计数器(周期性截断)
        # Windows 自愈:连续异常恢复计数,超过阈值就真正抛出
        self._recover_count = 0
        self._recover_reset_threshold = 64  # 连续 64 次恢复后放弃
        self._dwt_cyccnt_base = 0     # DWT cycle counter reset base
        self._stuck_pc = 0            # 卡死检测:上次 PC 页
        self._stuck_count = 0         # 卡死检测:同一 PC 范围连续计数
        self._stuck_logged = False    # 卡死检测:是否已打印警告
        self._loop_pc_page = 0        # 循环检测:当前 PC 页
        self._loop_start_icount = 0   # 循环检测:进入当前页的 icount
        self._list_iter_set = set()   # 循环检测:链表迭代器历史值
        self._list_break_count = 0    # 循环检测:累计断开次数(限流日志)
        self._stuck_loop_pc = 0       # 卡死检测:当前 PC 块
        self._stuck_loop_count = 0    # 卡死检测:同一块连续计数
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
        self.uc.hook_add(UC_HOOK_MEM_FETCH_UNMAPPED, self._hook_fetch_unmapped)
        self.uc.hook_add(UC_HOOK_INTR, self._hook_intr)

        # 预映射 EXC_RETURN 地址范围 (0xFFFFF000-0xFFFFFFFF)
        # 当固件执行 bx LR / pop {pc} 且目标为 EXC_RETURN 值时,PC 会跳到
        # 0xFFFFFFF1/9/D。预先映射此页并填入 "b ." 指令,然后用 code hook
        # 检测并处理异常返回。这避免了 fetch_unmapped 中 emu_stop() 不生效的问题。
        try:
            self.uc.mem_map(0xFFFFF000, 0x1000, UC_PROT_ALL)
            # 填入 "b ." (0xE7FE) 让 CPU 不跑飞
            self.uc.mem_write(0xFFFFF000, b"\xfe\xe7" * 2048)
        except Exception:
            pass  # 可能已映射
        # code hook 仅针对 EXC_RETURN 范围,性能开销极小
        from unicorn import UC_HOOK_CODE
        self.uc.hook_add(UC_HOOK_CODE, self._hook_exc_return,
                         begin=0xFFFFFFF0, end=0xFFFFFFFF)
        # vListInsert 入口钩子:自动检测并移除已在列表中的项,防止自引用损坏
        self._vlist_insert_addr = 0x08017FC2  # vListInsert 函数入口
        self._vlist_remove_count = 0
        self.uc.hook_add(UC_HOOK_CODE, self._hook_vlist_insert,
                         begin=self._vlist_insert_addr, end=self._vlist_insert_addr + 1)
        # 注意:不再注册全局 UC_HOOK_CODE(性能极差,每条指令回调 Python)
        # icount 通过 step(count=N) 的 N 累加估算

    # ---------- 固件加载 ----------
    def load_firmware(self, fw: FirmwareImage) -> None:
        self.firmware = fw
        # 写入 Flash 镜像
        self.uc.mem_write(FLASH_BASE, fw.data)
        # 地址 0 是 Flash 的别名 (真实 STM32 行为)
        # 同步 Flash 数据到地址 0,让 NULL 指针解引用返回真实硬件行为
        self.uc.mem_write(0x00000000, fw.data[:FLASH_SIZE])
        # 重置 SRAM 为 0x00 (crt0 会清零 BSS,拷贝 Data 段)
        self.uc.mem_write(SRAM1_BASE, b"\x00" * (SRAM1_SIZE + SRAM2_SIZE))
        # 重置状态
        self.icount = 0
        self.in_handler = 0
        self._pendsv_pending = False
        self._systick_pending = False
        self._list_iter_set = set()
        self._list_break_count = 0
        self._loop_pc_page = 0
        self._stuck_logged = False
        self._skip_current_instr = False
        self._warn_64bit_count = 0
        self._reg_truncate_counter = 0
        self._recover_count = 0
        # 设置初始 SP 与 PC
        self.uc.reg_write(UC_ARM_REG_SP, fw.initial_sp & 0xFFFFFFFF)
        # Cortex-M:PC 的 bit0 必须为 1 以表示 Thumb 状态
        self.uc.reg_write(UC_ARM_REG_PC, (fw.entry_point & 0xFFFFFFFE) | 1)
        # CPSR:Thumb(bit5)+ Supervisor 模式(bit0-4=0x13)
        self.uc.reg_write(UC_ARM_REG_CPSR, 0x00000023)
        # 关键:显式清零所有通用寄存器,防止 Windows 上 Unicorn
        # 初始寄存器值带 >32 位的垃圾位导致地址计算错误
        for reg in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                    UC_ARM_REG_R8, UC_ARM_REG_R12, UC_ARM_REG_LR):
            self.uc.reg_write(reg, 0)
        # MSP/PSP 也确保 32 位
        self.uc.reg_write(UC_ARM_REG_MSP, fw.initial_sp & 0xFFFFFFFF)
        self.uc.reg_write(UC_ARM_REG_PSP, 0)
        self.uc.reg_write(UC_ARM_REG_PRIMASK, 0)
        self.uc.reg_write(UC_ARM_REG_BASEPRI, 0)
        # 设置 VTOR 指向 Flash 起始(在 PPB 中)
        vtor = FLASH_BASE
        self.ppb[SCB_VTOR - PPB_BASE:SCB_VTOR - PPB_BASE + 4] = vtor.to_bytes(4, "little")

        # 外设合理初值:GPIOB PB11(DC) 默认低=命令模式,GPIOC PC9(RST) 默认高=解除复位
        self.gpiob.write(self.gpiob.ODR, 4, 0)
        self.gpioc.write(self.gpioc.ODR, 4, (1 << 9))
        # 为了兼容"固件未发 0xAF 开屏命令"的情况,默认强制打开显示
        self.display.turn_on()

        # 在屏幕上显示固件加载信息,让用户立刻看到屏幕有内容
        # 固件执行后写 LCD 时会覆盖这些内容
        self._paint_boot_screen(fw)

        # 通过 UART 输出加载信息
        if self.on_uart_tx:
            msg = f"\r\n[FlipperVM] Firmware loaded: {len(fw.data)} bytes\r\n"
            msg += f"[FlipperVM] SP=0x{fw.initial_sp:08X} PC=0x{fw.entry_point:08X}\r\n"
            msg += f"[FlipperVM] Press RUN to start execution\r\n"
            for ch in msg:
                self.on_uart_tx(ord(ch))

    # ---------- 启动画面 ----------
    _BOOT_FONT = {
        'A': ["01110","10001","10001","11111","10001","10001","10001"],
        'B': ["11110","10001","10001","11110","10001","10001","11110"],
        'C': ["01111","10000","10000","10000","10000","10000","01111"],
        'D': ["11110","10001","10001","10001","10001","10001","11110"],
        'E': ["11111","10000","10000","11110","10000","10000","11111"],
        'F': ["11111","10000","10000","11110","10000","10000","10000"],
        'G': ["01111","10000","10000","10111","10001","10001","01111"],
        'H': ["10001","10001","10001","11111","10001","10001","10001"],
        'I': ["01110","00100","00100","00100","00100","00100","01110"],
        'K': ["10001","10010","10100","11000","10100","10010","10001"],
        'L': ["10000","10000","10000","10000","10000","10000","11111"],
        'M': ["10001","11011","10101","10101","10001","10001","10001"],
        'N': ["10001","11001","10101","10011","10001","10001","10001"],
        'O': ["01110","10001","10001","10001","10001","10001","01110"],
        'P': ["11110","10001","10001","11110","10000","10000","10000"],
        'R': ["11110","10001","10001","11110","10100","10010","10001"],
        'S': ["01111","10000","10000","01110","00001","00001","11110"],
        'T': ["11111","00100","00100","00100","00100","00100","00100"],
        'U': ["10001","10001","10001","10001","10001","10001","01110"],
        'V': ["10001","10001","10001","10001","10001","01010","00100"],
        'W': ["10001","10001","10001","10101","10101","10101","01010"],
        'X': ["10001","10001","01010","00100","01010","10001","10001"],
        'Y': ["10001","10001","10001","01010","00100","00100","00100"],
        'Z': ["11111","00001","00010","00100","01000","10000","11111"],
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
        ' ': ["00000","00000","00000","00000","00000","00000","00000"],
        ':': ["00000","00100","00000","00000","00000","00100","00000"],
        '.': ["00000","00000","00000","00000","00000","00000","00100"],
        '!': ["00100","00100","00100","00100","00100","00000","00100"],
        '-': ["00000","00000","00000","11111","00000","00000","00000"],
        '/': ["00001","00010","00010","00100","01000","01000","10000"],
        'x': ["00000","00000","10001","01010","00100","01010","10001"],
    }

    def _paint_boot_screen(self, fw: FirmwareImage) -> None:
        """在 LCD 上画启动信息,让用户立刻看到屏幕有内容。"""
        d = self.display
        d.clear()
        W, H = d.width, d.height
        # 画边框
        for x in range(W):
            d.set_pixel(x, 0, 1)
            d.set_pixel(x, H - 1, 1)
        for y in range(H):
            d.set_pixel(0, y, 1)
            d.set_pixel(W - 1, y, 1)

        font = self._BOOT_FONT
        def draw_text(x, y, text):
            px = x
            for ch in text.upper():
                glyph = font.get(ch, font[' '])
                for gy, row in enumerate(glyph):
                    for gx, bit in enumerate(row):
                        if bit == '1' and 0 <= px+gx < W and 0 <= y+gy < H:
                            d.set_pixel(px + gx, y + gy, 1)
                px += 6

        size_kb = len(fw.data) // 1024
        draw_text(4, 4, "FlipperVM")
        draw_text(4, 16, f"FW {size_kb}KB")
        draw_text(4, 28, f"SP {fw.initial_sp:08X}")
        draw_text(4, 40, f"PC {fw.entry_point:08X}")
        draw_text(4, 52, "PRESS RUN")

    # ---------- 按键 ----------
    def set_button(self, name: str, pressed: bool) -> None:
        if name not in BUTTON_MAP:
            return
        gpio_base, pin_bit = BUTTON_MAP[name]
        gpio = self.peripherals[gpio_base]
        gpio.set_button(pin_bit, pressed)
        # pin_no = EXTI 线号 = GPIO 引脚号 (0-5) = NVIC IRQ 号 (0-5)
        pin_no = {1: 0, 2: 1, 4: 2, 8: 3, 0x10: 4, 0x20: 5}.get(
            pin_bit, max(0, pin_bit.bit_length() - 1)
        )
        if pressed:
            # 确保 NVIC ISER 使能了该 IRQ(即便固件还没初始化也能进中断)
            iser_idx = pin_no // 32
            iser_bit = 1 << (pin_no % 32)
            self.nvic_iser[iser_idx] |= iser_bit
            # 同时使能 EXTI IMR (中断屏蔽) 和 FTSR (下降沿触发)
            # 真实固件通常通过 HAL_GPIO_Init / HAL_EXTI_SetConfigLine 设置,
            # 但如果还没设置,我们手动兜底:
            exti_imr_off, exti_ftsr_off = 0x00, 0x0C  # EXTI_IMR / EXTI_FTSR1
            try:
                imr = int.from_bytes(self.exti.regs[exti_imr_off:exti_imr_off + 4], "little")
                self.exti.regs[exti_imr_off:exti_imr_off + 4] = \
                    (imr | (1 << pin_no)).to_bytes(4, "little")
                ftsr = int.from_bytes(self.exti.regs[exti_ftsr_off:exti_ftsr_off + 4], "little")
                self.exti.regs[exti_ftsr_off:exti_ftsr_off + 4] = \
                    (ftsr | (1 << pin_no)).to_bytes(4, "little")
            except Exception:
                pass
            # EXTI Pending Register 置位 (模拟下降沿到达)
            exti_pr1_off = 0x14
            try:
                self.exti.regs[exti_pr1_off:exti_pr1_off + 4] = \
                    (1 << pin_no).to_bytes(4, "little")
            except Exception:
                pass
            # 最后 pend 中断到 NVIC
            self._pend_irq(pin_no)

    # ---------- 主执行循环 ----------
    def _find_exc_return_addr(self, start_pc: int, max_scan: int = 256) -> int:
        """在 handler 代码中扫描 bx lr / pop {pc} 指令地址。

        返回第一个找到的异常返回指令地址,用于设为 emu_start 的 until 参数。
        这样 emu_start 会在异常返回指令之前停止,我们可以手动处理 EXC_RETURN。
        """
        try:
            code = bytes(self.uc.mem_read(start_pc, max_scan))
        except Exception:
            return 0
        for i in range(0, len(code) - 1, 2):
            hw = code[i] | (code[i + 1] << 8)
            # bx lr = 0x4770
            if hw == 0x4770:
                return start_pc + i
            # pop {pc} = 0xBDxx (bit 8 = PC)
            if (hw & 0xFF00) == 0xBD00:
                return start_pc + i
            # pop.w {pc} = 0xE8BD (32-bit, check next halfword bit 15)
            if hw == 0xE8BD and i + 3 < len(code):
                hw2 = code[i + 2] | (code[i + 3] << 8)
                if hw2 & 0x8000:  # bit 15 = PC
                    return start_pc + i
        return 0

    def _thumb_instr_size(self, pc: int) -> int:
        """返回 PC 处 Thumb 指令的大小 (2 或 4 字节)。

        Thumb-2 32 位指令的第一个半字 bits[15:11] 为:
          0b11101 (0xE800-0xEFFF), 0b11110 (0xF000-0xF7FF), 0b11111 (0xF800-0xFFFF)
        其他为 16 位指令。
        """
        try:
            code = bytes(self.uc.mem_read(pc & ~1, 2))
            hw = code[0] | (code[1] << 8)
            if (hw & 0xF800) >= 0xE800:
                return 4
            return 2
        except Exception:
            return 2  # 默认 2 字节

    def _truncate_registers(self) -> None:
        """将所有 ARM 寄存器截断到 32 位。

        Windows 上 Unicorn 可能不自动截断寄存器到 32 位,
        导致地址计算产生 >32 位的地址。定期调用此方法清除高位垃圾。
        """
        try:
            for reg in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                        UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                        UC_ARM_REG_R8, UC_ARM_REG_R12, UC_ARM_REG_LR,
                        UC_ARM_REG_SP, UC_ARM_REG_PC):
                val = self.uc.reg_read(reg)
                if val > 0xFFFFFFFF:
                    self.uc.reg_write(reg, val & 0xFFFFFFFF)
            # MSP/PSP 也截断
            msp = self.uc.reg_read(UC_ARM_REG_MSP)
            if msp > 0xFFFFFFFF:
                self.uc.reg_write(UC_ARM_REG_MSP, msp & 0xFFFFFFFF)
            psp = self.uc.reg_read(UC_ARM_REG_PSP)
            if psp > 0xFFFFFFFF:
                self.uc.reg_write(UC_ARM_REG_PSP, psp & 0xFFFFFFFF)
        except Exception:
            pass

    def _try_skip_instruction(self, pc: int, err_msg: str) -> bool:
        """尝试跳过当前指令以从访问违规中恢复。

        在 Windows 上,Unicorn 可能产生 >32 位地址访问导致崩溃。
        策略:
          1. PC 在真实 Flash 可执行区 (0x08000000-0x08100000):
             截断所有寄存器到 32 位,然后跳过当前指令 (PC += 指令长度)
          2. PC 无效 (0 / >32 位 / 不在 Flash):
             如果有固件,重置 PC=Reset_Handler, SP=initial_sp, 清零寄存器,
             让固件从头重启 —— 这能清除任何残留的 64 位垃圾值
          3. 没有固件:放弃

        返回 True 如果成功恢复 (无论跳过还是重置)。
        """
        # PC >32 位:截断后重新判断
        if pc > 0xFFFFFFFF:
            pc32 = pc & 0xFFFFFFFF
            self.uc.reg_write(UC_ARM_REG_PC, pc32 | 1)
            pc = pc32

        # 策略 1:PC 在真实 Flash 可执行区,跳过当前指令
        if 0x08000000 <= pc < 0x08100000:
            skip = self._thumb_instr_size(pc)
            new_pc = (pc + skip) | 1
            self.uc.reg_write(UC_ARM_REG_PC, new_pc)
            self._truncate_registers()
            if self.on_uart_tx and self._warn_64bit_count < 5:
                self._warn_64bit_count += 1
                msg = f"\r\n[VM] RECOVER: skip instr @0x{pc:08X} ({err_msg[:60]})\r\n"
                for ch in msg:
                    self.on_uart_tx(ord(ch))
            return True

        # 策略 2:PC 无效 (0 / 在向量表 / 在 SRAM 执行等) —— 重置到 Reset_Handler
        # 注意:地址 0 虽然映射了 Flash 别名,但 PC=0 通常意味着异常处理已把状态搞坏,
        # 在向量表里"跳过指令"只会越走越乱 (用户报告的 PC=0 死循环就是这种情况)。
        if self.firmware is not None:
            entry = (self.firmware.entry_point & 0xFFFFFFFE) | 1
            sp = self.firmware.initial_sp & 0xFFFFFFFF
            self.uc.reg_write(UC_ARM_REG_PC, entry)
            self.uc.reg_write(UC_ARM_REG_SP, sp)
            self.uc.reg_write(UC_ARM_REG_LR, 0xFFFFFFFF)
            self.uc.reg_write(UC_ARM_REG_MSP, sp)
            self.uc.reg_write(UC_ARM_REG_PSP, 0)
            for reg in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                        UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                        UC_ARM_REG_R8, UC_ARM_REG_R12):
                self.uc.reg_write(reg, 0)
            self.in_handler = 0
            self._pendsv_pending = False
            self._systick_pending = False
            if self.on_uart_tx and self._warn_64bit_count < 8:
                self._warn_64bit_count += 1
                msg = (f"\r\n[VM] RECOVER: PC=0x{pc:08X} invalid, "
                       f"reset to Reset_Handler 0x{entry:08X}\r\n")
                for ch in msg:
                    self.on_uart_tx(ord(ch))
            return True

        return False

    def step(self, n_instructions: int = 1000) -> None:
        """执行 N 条指令,期间检查 SysTick / PendSV / pending IRQ。

        关键:在异常 handler 中(in_handler > 0)时,扫描 handler 代码中的
        bx lr / pop {pc} 指令,用 emu_start 的 until 参数在其之前停止,
        然后手动处理 EXC_RETURN。这避免了 unicorn 内置 EXC_RETURN 处理的挂起。
        线程模式:批量执行(count=remaining),无超时。
        中断派发在每轮循环开始时进行,确保线程模式代码有机会执行。
        """
        remaining = n_instructions
        while remaining > 0:
            cur_pc = self.uc.reg_read(UC_ARM_REG_PC)
            cur_sp = self.uc.reg_read(UC_ARM_REG_SP)

            # 如果 PC 已经是 EXC_RETURN 值(上一轮遗留),先处理
            if 0xFFFFFFF0 <= cur_pc <= 0xFFFFFFFF and self.in_handler > 0:
                self._return_from_exception(cur_pc)
                continue

            # 在线程模式时,检查并派发 pending 中断
            # 关键:必须尊重 PRIMASK 和 BASEPRI 寄存器!
            # FreeRTOS 使用 PRIMASK 或 BASEPRI 屏蔽中断来保护临界区
            # (vListInsert / vListInsertEnd 等)。如果忽略这些寄存器,
            # SysTick/PendSV 会在临界区中触发,导致链表损坏。
            # - PRIMASK=1: 禁止所有可屏蔽中断
            # - BASEPRI>0: 屏蔽优先级 >= BASEPRI 的中断
            # Cortex-M 优先级:值越大优先级越低。
            if self.in_handler == 0:
                primask = self.uc.reg_read(UC_ARM_REG_PRIMASK)
                basepri = self.uc.reg_read(UC_ARM_REG_BASEPRI)
                # SysTick 和 PendSV 的优先级从 SHPR3 读取
                # SHPR1=0xE000ED18 (exc 4-7), SHPR2=0xE000ED1C (exc 8-11),
                # SHPR3=0xE000ED20 (exc 12-15): [31:24]=SysTick(15), [23:16]=PendSV(14)
                shpr3 = int.from_bytes(self.ppb[SCB_SHPR + 8 - PPB_BASE:SCB_SHPR + 12 - PPB_BASE], "little")
                systick_prio = (shpr3 >> 24) & 0xFF
                pendsv_prio = (shpr3 >> 16) & 0xFF

                # PRIMASK=1 时禁止所有中断
                if not primask:
                    # PendSV 优先检查(优先级号更低 = 更高优先级)
                    if self._pendsv_pending and (basepri == 0 or pendsv_prio < basepri):
                        self._pendsv_pending = False
                        self._fire_exception(EXC_PENDSV)
                        continue
                    elif self._systick_pending and (basepri == 0 or systick_prio < basepri):
                        self._systick_pending = False
                        if self.systick_ctrl & 0x2:
                            self._fire_exception(EXC_SYSTICK)
                            continue
                    # 也检查 NVIC pending(需要检查每个 IRQ 的优先级)
                    if basepri == 0:  # 简化:NVIC IRQ 只在 BASEPRI=0 时派发
                        for i in range(4):
                            pending = self.nvic_pending[i] & self.nvic_iser[i]
                            if pending and self.in_handler == 0:
                                irq = i * 32 + (pending & -pending).bit_length() - 1
                                self.nvic_pending[i] &= ~(1 << (irq % 32))
                                self._fire_exception(IRQ0_OFFSET + irq)
                                break
                if self.in_handler > 0:
                    continue  # 进入了 handler,重新循环

            begin = cur_pc | 1
            until = 0
            batch = remaining

            if self.in_handler > 0:
                # 在 handler 中:扫描 bx lr / pop {pc} 指令
                ret_addr = self._find_exc_return_addr(cur_pc)
                if ret_addr > 0:
                    until = ret_addr
                    batch = remaining
                else:
                    # 没找到返回指令,用小批量避免卡死
                    batch = min(remaining, 100)
            else:
                # 线程模式:限制批量大小,确保 DWT_CYCCNT 能及时更新。
                # 固件使用 DWT_CYCCNT 实现精确延时 (furi_hal_cortex_delay_us),
                # 如果批量太大,DWT_CYCCNT 在批量内不变化,延时循环无法退出。
                # 批量 2000 条指令:DWT_CYCCNT 增加 2000*8=16000,足以跳过大多数延时。
                batch = min(remaining, 2000)

            try:
                self.uc.emu_start(begin, until, timeout=0, count=batch)
                # 成功执行一批指令,清零恢复计数
                self._recover_count = 0
            except UcError as e:
                # Windows 兼容:Unicorn 在 Windows 上可能产生 >32 位地址访问
                # 导致 "access violation" 错误。尝试跳过当前指令继续执行。
                err_msg = str(e)
                if "access violation" in err_msg or "UC_ERR_WRITE_UNMAPPED" in err_msg \
                   or "UC_ERR_READ_UNMAPPED" in err_msg or "UC_ERR_FETCH_UNMAPPED" in err_msg \
                   or "UC_ERR_INSN_INVALID" in err_msg:
                    pc = self.uc.reg_read(UC_ARM_REG_PC)
                    # 先截断所有寄存器,清除可能导致 64 位地址的高位垃圾
                    self._truncate_registers()
                    if self._try_skip_instruction(pc, err_msg):
                        self._recover_count += 1
                        if self._recover_count < self._recover_reset_threshold:
                            # 恢复成功,继续执行(消耗 1 条指令配额)
                            executed = 1
                            self.icount += executed
                            remaining -= executed
                            self._check_systick(executed)
                            continue
                        # 连续恢复次数过多 —— 真正抛出,让上层处理
                        if self.on_uart_tx:
                            msg = (f"\r\n[FlipperVM] Too many recoveries "
                                   f"({self._recover_count}), giving up: {e}\r\n")
                            for ch in msg:
                                self.on_uart_tx(ord(ch))
                self.running = False
                if self.on_uart_tx:
                    pc = self.uc.reg_read(UC_ARM_REG_PC)
                    msg = f"\r\n[FlipperVM] CPU exception: {e}\r\n[FlipperVM] PC=0x{pc:08X}\r\n"
                    for ch in msg:
                        self.on_uart_tx(ord(ch))
                raise
            except Exception as e:
                self.running = False
                if self.on_uart_tx:
                    pc = self.uc.reg_read(UC_ARM_REG_PC)
                    msg = f"\r\n[FlipperVM] CPU exception: {e}\r\n[FlipperVM] PC=0x{pc:08X}\r\n"
                    for ch in msg:
                        self.on_uart_tx(ord(ch))
                raise

            # 检查是否需要跳过当前指令 (64位地址访问后由 _handle_64bit_access 设置)
            if self._skip_current_instr:
                self._skip_current_instr = False
                pc = self.uc.reg_read(UC_ARM_REG_PC)
                skip = self._thumb_instr_size(pc)
                new_pc = (pc + skip) | 1
                self.uc.reg_write(UC_ARM_REG_PC, new_pc)
                executed = 1
                self.icount += executed
                remaining -= executed
                self._check_systick(executed)
                # 跳过后截断所有寄存器到 32 位,清除可能残留的高位
                self._truncate_registers()
                continue

            new_pc = self.uc.reg_read(UC_ARM_REG_PC)
            new_sp = self.uc.reg_read(UC_ARM_REG_SP)

            # 周期性截断寄存器到 32 位 (每 5000 条指令)
            # 防止 Windows 上 Unicorn 寄存器高位积累垃圾值
            self._reg_truncate_counter += batch
            if self._reg_truncate_counter >= 5000:
                self._reg_truncate_counter = 0
                self._truncate_registers()

            # 检查是否到达了异常返回指令
            if until > 0 and new_pc == until and self.in_handler > 0:
                handled = self._simulate_exc_return_instr(new_pc)
                if handled:
                    executed = 1
                    self.icount += executed
                    remaining -= executed
                    self._check_systick(executed)
                    continue

            # 检查 PC 是否变成了 EXC_RETURN 值(其他路径)
            if 0xFFFFFFF0 <= new_pc <= 0xFFFFFFFF and self.in_handler > 0:
                self._return_from_exception(new_pc)
                executed = 1
                self.icount += executed
                remaining -= executed
                self._check_systick(executed)
                continue

            # 正常执行了 batch 条指令
            # 注意:不再检查 "PC 和 SP 都没变" 来判断卡死,
            # 因为固件在紧密循环中(如链表遍历)执行 batch 条后
            # PC 可能恰好回到起点,这是正常行为而非卡死。
            executed = batch
            self.icount += executed
            remaining -= executed
            self._check_systick(executed)

            # ===== 卡死循环检测与恢复 =====
            # 线程模式:使用 256B 块检测紧密循环
            # Handler 模式:使用指令计数超时检测
            if self.in_handler == 0:
                pc_block = new_pc & ~0xFF  # 256 字节块
                if pc_block == self._stuck_loop_pc:
                    self._stuck_loop_count += 1
                else:
                    self._stuck_loop_pc = pc_block
                    self._stuck_loop_count = 1

                if self._stuck_loop_count >= 30:  # ~60K 指令无进展
                    recovered = self._recover_stuck_loop(new_pc)
                    if recovered:
                        self._stuck_loop_pc = 0
                        self._stuck_loop_count = 0
            else:
                # Handler 模式:跟踪 handler 内执行的指令总数
                # 如果超过 100K 条还没返回,认为 handler 卡死
                self._handler_instr_count += executed
                if self._handler_instr_count >= 100000:
                    # 尝试 vListInsert 恢复
                    recovered = self._recover_stuck_loop(new_pc)
                    if recovered:
                        self._handler_instr_count = 0
                    elif self._exc_frame_stack:
                        # 强制从异常帧恢复,返回被中断的线程代码
                        frame_sp, exc_return = self._exc_frame_stack[-1]
                        try:
                            frame = self.uc.mem_read(frame_sp, 32)
                            r0, r1, r2, r3, r12, lr, pc_val, xpsr = struct.unpack("<IIIIIIII", frame)
                            # 验证恢复的 PC 在有效范围内
                            if 0x08000000 <= pc_val <= 0x08300000:
                                self.uc.reg_write(UC_ARM_REG_R0, r0)
                                self.uc.reg_write(UC_ARM_REG_R1, r1)
                                self.uc.reg_write(UC_ARM_REG_R2, r2)
                                self.uc.reg_write(UC_ARM_REG_R3, r3)
                                self.uc.reg_write(UC_ARM_REG_R12, r12)
                                self.uc.reg_write(UC_ARM_REG_LR, lr)
                                self.uc.reg_write(UC_ARM_REG_PC, pc_val | 1)
                                self.uc.reg_write(UC_ARM_REG_XPSR, xpsr & 0xF8000000 | (1 << 24))
                                new_sp = frame_sp + 32
                                if exc_return == 0xFFFFFFFD:
                                    self.uc.reg_write(UC_ARM_REG_PSP, new_sp)
                                else:
                                    self.uc.reg_write(UC_ARM_REG_MSP, new_sp)
                                self.uc.reg_write(UC_ARM_REG_SP, new_sp)
                                self._exc_frame_stack.pop()
                                self.in_handler -= 1
                                self._handler_instr_count = 0
                                # 清除 BASEPRI 和 SysTick pending
                                # handler 卡死说明 FreeRTOS 临界区状态损坏,
                                # 清除 BASEPRI 让 PendSV 能正常派发
                                self.uc.reg_write(UC_ARM_REG_BASEPRI, 0)
                                self._systick_pending = False
                                if self.on_uart_tx:
                                    msg = "[VM] Handler timeout, restored PC=0x%08X\n" % pc_val
                                    for ch in msg:
                                        self.on_uart_tx(ord(ch))
                        except Exception:
                            pass

    def _recover_stuck_loop(self, pc: int) -> bool:
        """检测并恢复卡死循环。

        对 vListInsert 中的自引用环 (pxNext 指向自身),将 item 完全重置
        (pxNext/pxPrevious/pxContainer=NULL),并修复列表为空列表,
        然后从 vListInsert 函数开头重新执行,让插入代码正确运行。
        对其他卡死循环,扫描前方 pop {.., pc} / bx lr 指令并跳转过去。

        返回 True 如果成功恢复。
        """
        # vListInsert 循环范围:0x08017FC0 - 0x08018010 (整个函数体)
        if 0x08017FC0 <= pc <= 0x08018010:
            r0 = self.uc.reg_read(UC_ARM_REG_R0)  # List_t *
            r1 = self.uc.reg_read(UC_ARM_REG_R1)  # ListItem_t * (new item)
            if r1 and 0x20000000 <= r1 < 0x20040000 and \
               r0 and 0x20000000 <= r0 < 0x20040000:
                try:
                    data = bytes(self.uc.mem_read(r1, 12))
                    xitemvalue, pxnext, pxprev = struct.unpack("<3I", data)
                    # 检测损坏:自引用或 pxNext 指向 list_end(已在列表末尾)或形成环
                    list_end = r0 + 8  # &xListEnd
                    is_corrupt = (pxnext == r1 or pxprev == r1 or
                                  pxnext == 0 or pxprev == 0)
                    # 也检测环:遍历 pxNext 链,如果回到 r1 或超过 32 步,视为环
                    if not is_corrupt and pxnext and 0x20000000 <= pxnext < 0x20040000:
                        cur = pxnext
                        for _ in range(32):
                            try:
                                nx = struct.unpack("<I", bytes(self.uc.mem_read(cur + 4, 4)))[0]
                                if nx == r1 or nx == cur:
                                    is_corrupt = True
                                    break
                                if nx == 0 or nx == list_end:
                                    break
                                cur = nx
                            except Exception:
                                break
                    if is_corrupt:
                        # 重置 item:pxNext=NULL, pxPrevious=NULL, pxContainer=NULL
                        self.uc.mem_write(r1 + 4, struct.pack("<I", 0))   # pxNext
                        self.uc.mem_write(r1 + 8, struct.pack("<I", 0))   # pxPrevious
                        self.uc.mem_write(r1 + 0x10, struct.pack("<I", 0)) # pxContainer

                        # 重置列表为空:
                        # List_t: [0]=uxNumberOfItems, [4]=pxIndex, [8]=xListEnd.xItemValue,
                        #         [12]=xListEnd.pxNext, [16]=xListEnd.pxPrevious
                        # xListEnd.xItemValue 必须设为 portMAX_DELAY (0xFFFFFFFF)!
                        # 否则 vListInsert 循环条件 pxNext->xItemValue <= xValueOfInsertion
                        # 在空列表上永远为 true (0 <= anything),导致死循环。
                        self.uc.mem_write(r0 + 0, struct.pack("<I", 0))           # uxNumberOfItems = 0
                        self.uc.mem_write(r0 + 4, struct.pack("<I", list_end))    # pxIndex = &xListEnd
                        self.uc.mem_write(list_end + 0, struct.pack("<I", 0xFFFFFFFF))  # xItemValue = portMAX_DELAY
                        self.uc.mem_write(list_end + 4, struct.pack("<I", list_end))    # end.pxNext = &xListEnd
                        self.uc.mem_write(list_end + 8, struct.pack("<I", list_end))    # end.pxPrevious = &xListEnd

                        # 从 vListInsert 函数开头重新执行 (0x08017FC2)
                        # R0=list, R1=item 仍然有效
                        # 需要恢复 push {r4, r5, lr} 的栈帧
                        # 实际上我们已经在函数内部,栈帧已存在
                        # 只需设 PC 到循环开始前的初始化点
                        # 0x08017FC4: ldr r4, [r1]  ; r4 = item->xItemValue
                        # 0x08017FC6: adds r3, r4, #1  ; 检查 xItemValue == 0xFFFFFFFF
                        # 0x08017FC8: bne 0x8017fe0  ; 如果不是,跳到循环
                        self.uc.reg_write(UC_ARM_REG_PC, 0x08017FC4 | 1)
                        if self.on_uart_tx and self._list_break_count < 3:
                            msg = "[VM] vListInsert: reset corrupt item 0x%08X\n" % r1
                            for ch in msg:
                                self.on_uart_tx(ord(ch))
                        self._list_break_count += 1
                        return True
                except Exception:
                    pass

        # 通用恢复:扫描前方 512 字节内的返回指令 (pop {.., pc} 或 bx lr)
        if 0x08000000 <= pc < 0x08100000:
            try:
                code = bytes(self.uc.mem_read(pc & ~1, 512))
                for i in range(0, len(code) - 4, 2):
                    hw = code[i] | (code[i + 1] << 8)
                    # bx lr (0x4770)
                    if hw == 0x4770:
                        target = (pc & ~1) + i
                        self.uc.reg_write(UC_ARM_REG_PC, target | 1)
                        return True
                    # pop {rlist, pc} (16-bit: 0xBDxx)
                    if (hw & 0xFF00) == 0xBD00:
                        target = (pc & ~1) + i
                        self.uc.reg_write(UC_ARM_REG_PC, target | 1)
                        return True
                    # pop.w {rlist, pc} (32-bit: 0xE8BD xxxx)
                    if hw == 0xE8BD and i + 3 < len(code):
                        target = (pc & ~1) + i
                        self.uc.reg_write(UC_ARM_REG_PC, target | 1)
                        return True
            except Exception:
                pass
        return False

    def _simulate_exc_return_instr(self, pc: int) -> bool:
        """手动模拟 bx lr / pop {pc} 指令,避免 unicorn 处理 EXC_RETURN。

        返回 True 如果成功处理了异常返回。
        """
        try:
            code = bytes(self.uc.mem_read(pc, 4))
        except Exception:
            return False
        hw = code[0] | (code[1] << 8)

        if hw == 0x4770:
            # bx lr
            lr = self.uc.reg_read(UC_ARM_REG_LR)
            if 0xFFFFFFF0 <= lr <= 0xFFFFFFFF:
                self._return_from_exception(lr)
                return True
            # 普通 bx lr(非异常返回)— 让 unicorn 正常执行
            try:
                self.uc.emu_start(pc | 1, 0, timeout=0, count=1)
            except Exception:
                pass
            return True

        if (hw & 0xFF00) == 0xBD00:
            # pop {rlist, pc} — 16 位 Thumb
            reg_list = hw & 0xFF  # bit 0-7 = R0-R7
            has_pc = bool(hw & 0x100)  # bit 8 = PC
            sp = self.uc.reg_read(UC_ARM_REG_SP)
            n_regs = bin(reg_list).count('1') + (1 if has_pc else 0)
            # 从栈上读取寄存器值
            try:
                data = self.uc.mem_read(sp, n_regs * 4)
            except Exception:
                return False
            vals = struct.unpack(f"<{n_regs}I", data)
            new_sp = sp + n_regs * 4
            # 恢复 R0-R7
            reg_idx = 0
            for i in range(8):
                if reg_list & (1 << i):
                    self.uc.reg_write(UC_ARM_REG_R0 + i, vals[reg_idx])
                    reg_idx += 1
            self.uc.reg_write(UC_ARM_REG_SP, new_sp)
            if has_pc:
                pc_val = vals[reg_idx]
                if 0xFFFFFFF0 <= pc_val <= 0xFFFFFFFF:
                    self._return_from_exception(pc_val)
                else:
                    self.uc.reg_write(UC_ARM_REG_PC, pc_val | 1)
            return True

        if hw == 0xE8BD:
            # pop.w {rlist, pc} — 32 位 Thumb-2
            hw2 = code[2] | (code[3] << 8)
            reg_list = (hw2 << 16) | hw  # 完整的寄存器列表
            # pop.w 的寄存器编码:hw=0xE8BD, hw2 的 bit 0-14 = R0-R14, bit 15 = PC
            regs = hw2 & 0x7FFF  # R0-R14
            has_pc = bool(hw2 & 0x8000)
            if not has_pc:
                # 不含 PC — 让 unicorn 正常执行
                try:
                    self.uc.emu_start(pc | 1, 0, timeout=0, count=1)
                except Exception:
                    pass
                return True
            sp = self.uc.reg_read(UC_ARM_REG_SP)
            n_regs = bin(regs).count('1') + 1  # +1 for PC
            try:
                data = self.uc.mem_read(sp, n_regs * 4)
            except Exception:
                return False
            vals = struct.unpack(f"<{n_regs}I", data)
            new_sp = sp + n_regs * 4
            reg_idx = 0
            for i in range(15):  # R0-R14
                if regs & (1 << i):
                    if i < 13:
                        self.uc.reg_write(UC_ARM_REG_R0 + i, vals[reg_idx])
                    elif i == 14:  # LR (R14)
                        self.uc.reg_write(UC_ARM_REG_LR, vals[reg_idx])
                    reg_idx += 1
            pc_val = vals[reg_idx]
            self.uc.reg_write(UC_ARM_REG_SP, new_sp)
            if 0xFFFFFFF0 <= pc_val <= 0xFFFFFFFF:
                self._return_from_exception(pc_val)
            else:
                self.uc.reg_write(UC_ARM_REG_PC, pc_val | 1)
            return True

        return False

    def _dispatch_post_exception(self):
        """在异常返回后检查并派发 pending 中断。"""
        if self._pendsv_pending:
            self._pendsv_pending = False
            self._fire_exception(EXC_PENDSV)
        elif self._systick_pending:
            self._systick_pending = False
            if self.systick_ctrl & 0x2:
                self._fire_exception(EXC_SYSTICK)
        self._dispatch_pending_irq()

    def _advance_periph_timers(self, n_executed: int) -> None:
        """推进 LPTIM 等自由运行定时器计数。"""
        # LPTIM 输入时钟通常 32.768kHz 或 LSI/4,但为了防止固件等太久,
        # 这里按 1:1 与 CPU 同步推进(等效非常快的 LPTIM 时钟,尽快触发 ARRM 唤醒)。
        try:
            self.lptim1.advance(n_executed)
        except Exception:
            pass
        try:
            self.lptim2.advance(n_executed)
        except Exception:
            pass

    def _check_systick(self, n_executed: int) -> None:
        """检查 SysTick 是否到期并触发异常。"""
        # 先推进 LPTIM:保证即便还没到 FreeRTOS 配置 SysTick,
        # 早期的 LPTIM 睡眠(bootloader/初始化阶段)也能被唤醒
        self._advance_periph_timers(n_executed)
        if (self.systick_ctrl & 0x1) and self.systick_load:
            self.systick_countdown -= n_executed
            if self.systick_countdown <= 0:
                self.systick_countdown = self.systick_load
                self.systick_val = 0
                self._systick_countflag = True
                if self.systick_ctrl & 0x2:  # TICKINT
                    # 检查中断屏蔽:PRIMASK=1 禁止所有可屏蔽中断
                    # BASEPRI 屏蔽优先级 >= BASEPRI 的中断
                    primask = self.uc.reg_read(UC_ARM_REG_PRIMASK)
                    basepri = self.uc.reg_read(UC_ARM_REG_BASEPRI)
                    # SysTick 优先级 (SHPR3 [31:24])
                    # SHPR3 = SCB_SHPR + 8 = 0xE000ED20
                    shpr3 = int.from_bytes(
                        self.ppb[SCB_SHPR + 8 - PPB_BASE:SCB_SHPR + 12 - PPB_BASE], "little")
                    systick_prio = (shpr3 >> 24) & 0xFF
                    if primask or (basepri > 0 and systick_prio >= basepri):
                        # 中断被屏蔽,设为 pending
                        self._systick_pending = True
                    elif self.in_handler == 0:
                        self._fire_exception(EXC_SYSTICK)
                    else:
                        # 在 handler 中时不立即触发,设为 pending
                        self._systick_pending = True

    # ---------- 异常机制 ----------
    def _vector_address(self, exception: int) -> int:
        vtor = int.from_bytes(self.ppb[SCB_VTOR - PPB_BASE:SCB_VTOR - PPB_BASE + 4], "little")
        tbl_off = exception * 4
        val = int.from_bytes(self.uc.mem_read(vtor + tbl_off, 4), "little")
        # 保留 Thumb 位(bit0=1),否则 unicorn 当 ARM 解码
        return val | 1

    def _fire_exception(self, exception: int) -> None:
        """手动进入异常:压栈 8 字,设置 LR=EXC_RETURN,PC=向量。

        Cortex-M 硬件行为:
          - 线程模式用 PSP 时:帧压入 PSP,切换到 MSP 进 handler,EXC_RETURN=0xFFFFFFFD
          - 线程模式用 MSP 时:帧压入 MSP,继续用 MSP,EXC_RETURN=0xFFFFFFF9
          - 已在 handler 模式:帧压入 MSP,EXC_RETURN=0xFFFFFFF1(返回 handler)
        """
        if self.in_handler >= 4:
            return  # 嵌套过深,忽略
        # Cortex-M:异常进入时,如果线程模式使用 PSP,则帧压入 PSP,然后切到 MSP
        if self.in_handler == 0:
            cur_sp = self.uc.reg_read(UC_ARM_REG_SP)
            msp = self.uc.reg_read(UC_ARM_REG_MSP)
            psp = self.uc.reg_read(UC_ARM_REG_PSP)
            # 防御:如果当前 SP 不在有效 SRAM 范围内(如 PSP 未初始化=0),
            # 回退到 MSP;如果 MSP 也不有效,跳过异常避免崩溃
            # 注意:SP 可以等于 SRAM 末尾(栈向下增长,首次压栈 SP-32 仍在范围内)
            sram_end = SRAM1_BASE + SRAM1_SIZE + SRAM2_SIZE
            if cur_sp < SRAM1_BASE or cur_sp > sram_end:
                cur_sp = msp  # 回退到 MSP
            if cur_sp < SRAM1_BASE or cur_sp > sram_end:
                return  # MSP 和 PSP 都无效,跳过异常
            if cur_sp == psp and psp != msp:
                # 线程用 PSP → 帧压入 PSP,handler 用 MSP
                frame_sp = psp - 32
                self._exc_return_psp = True
                exc_return = 0xFFFFFFFD
            else:
                # 线程用 MSP → 帧压入 MSP
                frame_sp = msp - 32
                self._exc_return_psp = False
                exc_return = 0xFFFFFFF9
        else:
            # 已在 handler 模式,帧压入 MSP
            frame_sp = self.uc.reg_read(UC_ARM_REG_MSP) - 32
            self._exc_return_psp = False
            exc_return = 0xFFFFFFF1

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
        self.uc.mem_write(frame_sp, frame)

        if exc_return == 0xFFFFFFFD:
            # 帧在 PSP 上,更新 PSP;handler 用 MSP
            self.uc.reg_write(UC_ARM_REG_PSP, frame_sp)
            self.uc.reg_write(UC_ARM_REG_SP, msp)
        else:
            # 帧在 MSP 上
            self.uc.reg_write(UC_ARM_REG_MSP, frame_sp)
            self.uc.reg_write(UC_ARM_REG_SP, frame_sp)

        self.uc.reg_write(UC_ARM_REG_LR, exc_return)
        handler_addr = self._vector_address(exception)
        self.uc.reg_write(UC_ARM_REG_PC, handler_addr)
        self.in_handler += 1
        self._exc_frame_stack.append((frame_sp, exc_return))
        self._handler_instr_count = 0
        if exception >= IRQ0_OFFSET:
            irq = exception - IRQ0_OFFSET
            self.nvic_active[irq // 32] |= 1 << (irq % 32)

    def _return_from_exception(self, exc_return: int) -> None:
        """从异常返回:出栈 8 字恢复上下文。

        Cortex-M 硬件行为:
          - EXC_RETURN=0xFFFFFFFD:从 PSP 出栈,返回 thread 模式用 PSP
            (FreeRTOS SVC handler 会修改 PSP 指向新任务栈,这里必须从 PSP 读帧)
          - EXC_RETURN=0xFFFFFFF9:从 MSP 出栈,返回 thread 模式用 MSP
          - EXC_RETURN=0xFFFFFFF1:从 MSP 出栈,返回 handler 模式用 MSP
        """
        if exc_return == 0xFFFFFFFD:
            sp = self.uc.reg_read(UC_ARM_REG_PSP)
        else:
            sp = self.uc.reg_read(UC_ARM_REG_MSP)
        frame = self.uc.mem_read(sp, 32)
        r0, r1, r2, r3, r12, lr, pc, xpsr = struct.unpack("<IIIIIIII", frame)
        # 验证恢复的 PC 在有效 Flash 范围内
        # 如果 PC 不在 Flash (如指向 SRAM=0x20000000),说明任务栈已损坏
        # 此时跳到当前 pxCurrentTCB 指向的任务可能也无济于事,
        # 直接跳到空闲任务或 Reset_Handler
        if not (0x08000000 <= pc < 0x08300000):
            # PC 无效 — 栈损坏。尝试用 Reset_Handler 恢复
            if self.on_uart_tx and self._warn_64bit_count < 10:
                self._warn_64bit_count += 1
                msg = (f"\r\n[VM] Bad PC in exc return: 0x{pc:08X} "
                       f"SP=0x{sp:08X} exc_ret=0x{exc_return:08X}\r\n")
                for ch in msg:
                    self.on_uart_tx(ord(ch))
            if self.firmware is not None:
                pc = (self.firmware.entry_point & 0xFFFFFFFE)
                lr = 0xFFFFFFFF
                # 重置栈
                sp = self.firmware.initial_sp & 0xFFFFFFFF
                self.uc.reg_write(UC_ARM_REG_MSP, sp)
                self.uc.reg_write(UC_ARM_REG_PSP, 0)
                self.in_handler = 0
                self._exc_frame_stack.clear()
                self._pendsv_pending = False
                self._systick_pending = False
                self.uc.reg_write(UC_ARM_REG_PC, pc | 1)
                self.uc.reg_write(UC_ARM_REG_SP, sp)
                self.uc.reg_write(UC_ARM_REG_LR, lr)
                self.uc.reg_write(UC_ARM_REG_BASEPRI, 0)
                self.uc.reg_write(UC_ARM_REG_PRIMASK, 0)
                return
        self.uc.reg_write(UC_ARM_REG_R0, r0)
        self.uc.reg_write(UC_ARM_REG_R1, r1)
        self.uc.reg_write(UC_ARM_REG_R2, r2)
        self.uc.reg_write(UC_ARM_REG_R3, r3)
        self.uc.reg_write(UC_ARM_REG_R12, r12)
        self.uc.reg_write(UC_ARM_REG_LR, lr)
        # Cortex-M:返回 PC 必须保留 Thumb 位(bit0=1),否则 unicorn 当 ARM 解码
        self.uc.reg_write(UC_ARM_REG_PC, pc | 1)
        self.uc.reg_write(UC_ARM_REG_XPSR, xpsr & 0xF8000000 | (1 << 24))
        sp += 32
        if exc_return == 0xFFFFFFFD:
            self.uc.reg_write(UC_ARM_REG_PSP, sp)
            self.uc.reg_write(UC_ARM_REG_SP, sp)
        else:
            self.uc.reg_write(UC_ARM_REG_MSP, sp)
            self.uc.reg_write(UC_ARM_REG_SP, sp)
        if self.in_handler > 0:
            self.in_handler -= 1
        if self._exc_frame_stack:
            self._exc_frame_stack.pop()

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
            # 防御:val 可能 >32 位 (理论上不会,但保险起见)
            write_size = min(size, 4)
            val_masked = val & ((1 << (8 * write_size)) - 1)
            try:
                uc.mem_write(address & ~0x3, val_masked.to_bytes(write_size, "little"))
            except Exception:
                pass

    def _hook_mem_write(self, uc, access, address, size, value, user_data):
        per = self._find_peripheral(address)
        if per is not None:
            off = address - per.base
            per.write(off, size, value)
            # 写入影子内存(对齐到 4 字节),避免后续读回污染
            # 关键:value 可能 >32 位 (Unicorn 在 Windows 上可能传递 64 位值)
            # 必须先掩码,且 to_bytes 的长度必须与掩码后的值匹配
            write_size = min(size, 4)
            val_masked = value & ((1 << (8 * write_size)) - 1)
            try:
                uc.mem_write(address & ~0x3, val_masked.to_bytes(write_size, "little"))
            except Exception:
                pass

    def _find_peripheral(self, address: int) -> Optional[Peripheral]:
        # Find the most specific peripheral (smallest size) that contains the address.
        # This prevents overlap issues where a large peripheral shadow covers a smaller one.
        best = None
        for base, per in self.peripherals.items():
            if per.base <= address < per.base + per.size:
                if best is None or per.size < best.size:
                    best = per
        return best

    # ---------- PPB(NVIC/SysTick/SCB)----------
    def _safe_write_shadow(self, uc, address, val, size=4):
        """安全地把值写入影子内存,自动掩码到 32 位防止 OverflowError。"""
        write_size = min(size, 4)
        val_masked = val & ((1 << (8 * write_size)) - 1)
        try:
            uc.mem_write(address & ~0x3, val_masked.to_bytes(write_size, "little"))
        except Exception:
            pass

    def _hook_ppb_read(self, uc, access, address, size, value, user_data):
        off = address - PPB_BASE
        # SysTick
        if SYSTICK_BASE <= address < SYSTICK_BASE + 0x10:
            val = self._systick_read(address - SYSTICK_BASE, size)
            self._safe_write_shadow(uc, address, val, size)
            return
        # DWT (Data Watchpoint and Trace)
        if DWT_BASE <= address < DWT_BASE + 0x20:
            val = self._dwt_read(address - DWT_BASE, size)
            self._safe_write_shadow(uc, address, val, size)
            return
        # DEMCR: TRCENA bit always set (DWT enabled)
        if address == DEMCR:
            val = int.from_bytes(self.ppb[off:off + size], "little")
            val |= DEMCR_TRCENA
            self._safe_write_shadow(uc, address, val, size)
            return
        # NVIC ISER/ISPR/ICPR 寄存器
        if NVIC_BASE <= address < NVIC_BASE + 0x300:
            val = self._nvic_read(address - NVIC_BASE, size)
            self._safe_write_shadow(uc, address, val, size)
            return
        # SCB_ICSR: 返回 PendSV/SysTick pending 状态
        if address == SCB_ICSR:
            val = int.from_bytes(self.ppb[off:off + size], "little")
            if self._pendsv_pending:
                val |= SCB_ICSR_PENDSVSET
            if self._systick_pending:
                val |= SCB_ICSR_PENDSTSET
            self._safe_write_shadow(uc, address, val, size)
            return
        # SCB_VTOR: 固件 SystemInit 会写 0 清零,但在真实硬件上 VTOR=0 表示
        # 使用默认启动地址(Flash 基址 0x08000000)。始终返回 FLASH_BASE。
        if address == SCB_VTOR:
            self._safe_write_shadow(uc, address, FLASH_BASE, size)
            return
        # SCB_CCR: 确保 STACKALIGN=1 (8字节栈对齐), UNALIGN_TRP=1 (未对齐访问触发异常)
        if address == SCB_CCR:
            val = int.from_bytes(self.ppb[off:off + size], "little")
            val |= (1 << 9) | (1 << 3)  # STKALIGN | UNALIGN_TRP
            self._safe_write_shadow(uc, address, val, size)
            return
        # SCB_SHPR (System Handler Priority Register): 返回存储的优先级
        if SCB_SHPR <= address < SCB_SHPR + 0xC:
            val = int.from_bytes(self.ppb[off:off + size], "little")
            self._safe_write_shadow(uc, address, val, size)
            return
        # CPACR (Coprocessor Access Control): 返回 FP FUll Access (0xF00000)
        if address == 0xE000ED88:
            val = int.from_bytes(self.ppb[off:off + size], "little")
            val |= 0x00F00000  # CP10/CP11 full access
            self._safe_write_shadow(uc, address, val, size)
            return
        # SCB 其它
        val = int.from_bytes(self.ppb[off:off + size], "little")
        self._safe_write_shadow(uc, address, val, size)

    def _hook_ppb_write(self, uc, access, address, size, value, user_data):
        # 防御:value 可能 >32 位 (Unicorn Windows bug),先掩码
        value &= 0xFFFFFFFF
        off = address - PPB_BASE
        if SYSTICK_BASE <= address < SYSTICK_BASE + 0x10:
            self._systick_write(address - SYSTICK_BASE, size, value)
            return
        # DWT: handle CYCCNT writes (firmware writes 0 to reset counter)
        if DWT_BASE <= address < DWT_BASE + 0x20:
            self._dwt_write(address - DWT_BASE, size, value)
            return
        if NVIC_BASE <= address < NVIC_BASE + 0x300:
            self._nvic_write(address - NVIC_BASE, size, value)
            return
        # SCB_ICSR: FreeRTOS 写 PENDSVSET 触发 PendSV, 写 PENDSTSET 触发 SysTick
        if address == SCB_ICSR:
            if value & SCB_ICSR_PENDSVSET:
                self._pendsv_pending = True
            if value & SCB_ICSR_PENDSTSET:
                self._systick_pending = True
            # 不存储 PENDSVSET/PENDSTSET 位(它们是 write-1-to-set, 读取时反映 pending 状态)
            self.ppb[off:off + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")
            return
        # SCB_VTOR: 固件 SystemInit 写 0 清零,但 VTOR=0 在真实硬件上表示
        # 使用默认 Flash 基址。始终保持 FLASH_BASE。
        if address == SCB_VTOR:
            self.ppb[off:off + 4] = FLASH_BASE.to_bytes(4, "little")
            return
        self.ppb[off:off + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")

    def _systick_read(self, off, size):
        if off == 0x00:   # CTRL — bit 16 = COUNTFLAG (set when timer hits 0, cleared on read)
            val = self.systick_ctrl
            if self._systick_countflag:
                val |= (1 << 16)
                self._systick_countflag = False  # Reading CTRL clears COUNTFLAG
            return val
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

    # ---------- DWT (Data Watchpoint and Trace) ----------
    # 固件使用 DWT_CYCCNT 实现精确延时 (furi_hal_cortex_delay_us)
    # 必须返回递增值,否则延时循环永远无法退出
    # 使用 8x 乘数:真实 Cortex-M4 @64MHz 每条指令约 1 cycle,
    # 但考虑流水线停顿/内存等待,有效 CPI 约 4-8,这里取 8 加速仿真
    DWT_CYCCNT_MULTIPLIER = 8

    def _dwt_read(self, off, size):
        if off == 0x00:   # DWT_CTRL
            # CYCCNTENA=1 (bit0), NUMCOMP=1 (bits[28:31] at least 1 comparator)
            return 0x10000001
        if off == 0x04:   # DWT_CYCCNT — 返回自上次重置以来的周期数
            return ((self.icount - self._dwt_cyccnt_base) * self.DWT_CYCCNT_MULTIPLIER) & 0xFFFFFFFF
        if off == 0x1C:   # DWT_PCSR — 返回当前 PC
            return self.uc.reg_read(UC_ARM_REG_PC)
        # 其它 DWT 寄存器返回 0
        return 0

    def _dwt_write(self, off, size, value):
        # 固件写 DWT_CYCCNT=0 来重置计数器
        if off == 0x04:
            # 重置 cycle counter 基准
            self._dwt_cyccnt_base = self.icount
        # DWT_CTRL 写入忽略(CYCCNTENA 始终为 1)

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
        # 对所有未映射访问,映射页面并填充 0xFF。
        # 返回 0xFF 而非 0x00 的原因:
        #   0xFFFFFFFF 是 FreeRTOS 的 portMAX_DELAY,也是链表末尾哨兵值。
        #   当固件遍历损坏的链表(节点指针为 0x8BADF00D 等)时,
        #   读取 0xFFFFFFFF 会使链表遍历循环终止,避免无限循环。

        # 关键修复:Windows 上 Unicorn 可能不把寄存器截断到 32 位,
        # 导致固件计算出 >32 位的地址 (如 0x1514A10D010)。
        # 此时 mem_map 在 64 位地址上可能失败,需要特殊处理。
        if address > 0xFFFFFFFF:
            return self._handle_64bit_access(uc, access, address, size, value)

        # 判断是否为写访问:
        # Unicorn 1.x: UC_MEM_WRITE_UNMAPPED = 2
        # Unicorn 2.x: UC_MEM_WRITE_UNMAPPED = 20
        is_write = access in (2, 20, UC_MEM_WRITE_UNMAPPED)

        try:
            page = address & ~0xFFF
            uc.mem_map(page, 0x1000, UC_PROT_ALL)
            # 填充 0xFF (而非默认的 0x00)
            uc.mem_write(page, b"\xff" * 0x1000)
            if is_write:
                write_size = min(size, 4)
                val_masked = value & ((1 << (8 * write_size)) - 1)
                uc.mem_write(address, val_masked.to_bytes(write_size, "little"))
            return True
        except Exception:
            return False

    def _handle_64bit_access(self, uc, access, address, size, value):
        """处理 Windows 上 Unicorn 产生的 >32 位地址访问。

        策略:
        1. 尝试在完整 64 位地址上映射页面 (Linux 上可行)
        2. 如果失败,截断到 32 位,在 32 位地址上处理
        3. 调用 emu_stop() 阻止 Unicorn 重试 64 位地址
        4. step() 中会检测 emu_stop 并推进 PC
        """
        is_write = access in (2, 20, UC_MEM_WRITE_UNMAPPED)
        addr32 = address & 0xFFFFFFFF

        # 策略 1:尝试在 64 位地址上映射
        try:
            page = address & ~0xFFF
            uc.mem_map(page, 0x1000, UC_PROT_ALL)
            uc.mem_write(page, b"\xff" * 0x1000)
            if is_write:
                write_size = min(size, 4)
                val_masked = value & ((1 << (8 * write_size)) - 1)
                uc.mem_write(address, val_masked.to_bytes(write_size, "little"))
            return True
        except Exception:
            pass

        # 策略 2:64 位映射失败,在截断的 32 位地址上处理
        # 先确保 32 位地址所在的页面已映射
        page32 = addr32 & ~0xFFF
        try:
            uc.mem_map(page32, 0x1000, UC_PROT_ALL)
            uc.mem_write(page32, b"\xff" * 0x1000)
        except Exception:
            pass  # 页面可能已映射

        # 在 32 位地址上执行读/写
        if is_write:
            write_size = min(size, 4)
            val_masked = value & ((1 << (8 * write_size)) - 1)
            try:
                uc.mem_write(addr32, val_masked.to_bytes(write_size, "little"))
            except Exception:
                pass

        # 关键:调用 emu_stop() 阻止 Unicorn 在 64 位地址上重试
        # step() 中会检测 _emu_stopped_early 并推进 PC 跳过当前指令
        self._emu_stopped_early = True
        self._skip_current_instr = True
        try:
            uc.emu_stop()
        except Exception:
            pass

        # 记录此事件用于诊断
        if self.on_uart_tx and getattr(self, '_warn_64bit_count', 0) < 3:
            self._warn_64bit_count = getattr(self, '_warn_64bit_count', 0) + 1
            pc = uc.reg_read(UC_ARM_REG_PC)
            msg = f"\r\n[VM] WARN: 64-bit addr 0x{address:X} -> 0x{addr32:08X} (PC=0x{pc:08X})\r\n"
            for ch in msg:
                self.on_uart_tx(ord(ch))

        return True

    def _hook_fetch_unmapped(self, uc, access, address, size, value, user_data):
        """代码取指从不映射地址(如固件跳转到 NULL/地址 0)。

        特殊处理:如果地址在 EXC_RETURN 范围(0xFFFFFFF0-0xFFFFFFFF),
        说明固件正在从异常返回(bx LR / pop {pc},LR/栈值为 EXC_RETURN)。
        此时调用 _return_from_exception 处理返回,并停止当前 emu_start,
        让 step() 从新的 PC 重新启动。
        """
        # Windows 兼容:>32 位地址 (Unicorn 可能不截断寄存器)
        # 截断到 32 位并重定向 PC
        if address > 0xFFFFFFFF:
            addr32 = address & 0xFFFFFFFF
            # 设置 PC 到截断的 32 位地址
            self.uc.reg_write(UC_ARM_REG_PC, addr32 | 1)
            self._emu_stopped_early = True
            self._skip_current_instr = False  # 不跳过,而是从新 PC 重新执行
            try:
                uc.emu_stop()
            except Exception:
                pass
            if self.on_uart_tx and getattr(self, '_warn_64bit_count', 0) < 3:
                self._warn_64bit_count = getattr(self, '_warn_64bit_count', 0) + 1
                msg = f"\r\n[VM] WARN: 64-bit fetch 0x{address:X} -> 0x{addr32:08X}\r\n"
                for ch in msg:
                    self.on_uart_tx(ord(ch))
            return True

        # 检查是否是 EXC_RETURN 值 (0xFFFFFFF1/0xFFFFFFF9/0xFFFFFFFD)
        if 0xFFFFFFF0 <= address <= 0xFFFFFFFF:
            lr = uc.reg_read(UC_ARM_REG_LR)
            # LR 可能就是 EXC_RETURN (bx lr 场景)
            # 或者栈上保存了 EXC_RETURN (pop {pc} 场景)
            exc_ret = lr if (lr & 0xFF000000 == 0xFF000000) else address
            if exc_ret & 0xFF000000 == 0xFF000000 and self.in_handler > 0:
                # 从异常返回
                self._return_from_exception(exc_ret)
                # 必须停止 emu_start,否则 unicorn 会无限重试同一地址的取指
                self._emu_stopped_early = True
                uc.emu_stop()
                return True
        try:
            page = address & ~0xFFF
            uc.mem_map(page, 0x1000, UC_PROT_ALL)
            # 填入 "b ." (branch to self = 0xE7FE) 的 Thumb 编码,让 CPU 停在原地
            uc.mem_write(page, b"\xfe\xe7" * 2048)
            return True
        except Exception:
            return False

    # ---------- EXC_RETURN 代码钩子 ----------
    def _hook_exc_return(self, uc, address, size, user_data):
        """当 PC 进入 EXC_RETURN 范围 (0xFFFFFFF0-0xFFFFFFFF) 时触发。

        这意味着固件执行了 bx LR / pop {pc},目标是 EXC_RETURN 值。
        在这里处理异常返回,然后停止 emu_start 让 step() 重启。
        """
        if self.in_handler > 0:
            # address 是当前 PC(已被设为 EXC_RETURN 值)
            # 使用 address 作为 EXC_RETURN 值
            self._return_from_exception(address)
            self._emu_stopped_early = True
            uc.emu_stop()

    def _hook_vlist_insert(self, uc, address, size, user_data):
        """vListInsert 入口钩子:仅在检测到自引用环时清理,避免死循环。

        FreeRTOS 正确用法是先 uxListRemove 再 vListInsert。如果临界区保护失效,
        item 仍带有旧的 pxNext/pxPrevious,vListInsert 会创建自引用环导致死循环。

        策略:
          - 只在 item->pxNext == item 或 pxPrevious == item (真正的自引用环) 时干预
          - 不因为 pxContainer != NULL 就移除 (那是正常的"已在列表中"状态,
            FreeRTOS 会先用 uxListRemove 再 vListInsert,这是正常流程)
          - 不跳过函数执行,让固件代码正常完成插入
        """
        r0 = uc.reg_read(UC_ARM_REG_R0)  # List_t *
        r1 = uc.reg_read(UC_ARM_REG_R1)  # ListItem_t * (new item)
        if not (r1 and 0x20000000 <= r1 < 0x20040000):
            return
        if not (r0 and 0x20000000 <= r0 < 0x20040000):
            return

        try:
            data = bytes(uc.mem_read(r1, 20))
            xvalue, pxnext, pxprev, pvowner, pxcontainer = struct.unpack("<5I", data)
        except Exception:
            return

        # 只检测真正的自引用环 (pxNext==item 或 pxPrevious==item)
        # 这种情况 vListInsert 会无限循环,必须干预
        if pxnext != r1 and pxprev != r1:
            return  # 没有自引用环,让固件正常执行

        # 检测到自引用环 — 清理 item 指针,让 vListInsert 正常插入
        if self.on_uart_tx and self._warn_64bit_count < 20:
            self._warn_64bit_count += 1
            msg = (f"\x0a[VM] self-loop in item 0x{r1:08X}, cleaning\x0a")
            for ch in msg:
                self.on_uart_tx(ord(ch))

        # 清理自引用指针
        uc.mem_write(r1 + 4, struct.pack("<I", 0))   # pxNext = NULL
        uc.mem_write(r1 + 8, struct.pack("<I", 0))   # pxPrevious = NULL
        # 注意:不设置 pxContainer,让 vListInsert 正常设置它
        # 不跳过函数 — 让固件代码完成正常的插入逻辑

    # ---------- 中断钩子(处理 SVC/PendSV/EXC_RETURN)----------
    def _hook_intr(self, uc, intno, user_data):
        lr = uc.reg_read(UC_ARM_REG_LR)
        # LR 是 EXC_RETURN 值 → 当前是从异常返回(我们的手动 handler 里 BX LR 触发)
        if lr & 0xFF000000 == 0xFF000000:
            self._return_from_exception(lr)
            return
        # SVC 异常:FreeRTOS 用 SVC 启动第一个任务和系统调用
        if intno == UC_ARM_EXCP_SWI:
            self._fire_exception(EXC_SVCALL)
            return
        # 其他异常(HardFault 等):记录但不递归 fire
        return
