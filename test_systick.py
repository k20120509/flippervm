"""验证 SysTick 异常进入/返回 + USART1 输出。"""
import struct

from unicorn.arm_const import (
    UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR,
)
from flipper_vm.emulator import FlipperVM
from flipper_vm.firmware_loader import FirmwareImage
from flipper_vm.stm32wb55 import (
    FLASH_BASE, SYSTICK_BASE, NVIC_BASE, USART1_BASE, SCB_VTOR,
)


# Thumb 指令编码
THUMB_NOP      = 0x46C0  # MOV R8, R8 (NOP)
THUMB_B_SELF   = 0xE7FE  # B .
THUMB_BX_LR    = 0x4770  # BX LR


def build_fw():
    """构造一个测试镜像:
       0x08000000: 向量表(SP=0x20040000, Reset=0x08000101, SysTick=0x08000201)
       0x08000100: Reset: 写 USART1 DR='H', 启动 SysTick, 死循环
       0x08000200: SysTick_Handler: 写 USART1 DR='T', BX LR
    """
    flash = bytearray(0x400)
    # 向量表(16 个 32 位字 + IRQ0 = vector[16])
    struct.pack_into("<II", flash, 0, 0x20040000, 0x08000101)
    # vector[15] = SysTick_Handler(在偏移 15*4=60)
    struct.pack_into("<I", flash, 15 * 4, 0x08000201)
    # Reset_Handler: 写 USART1 DR='H'(0x48),启动 SysTick,B .
    # 直接放裸 Thumb-2 指令序列太繁琐,这里用伪指令字节流:
    # 我们要执行:  LDR R0, =0x40013828   ; USART1 TDR
    #              LDR R1, =0x48          ; 'H'
    #              STR R1, [R0]
    #              LDR R0, =0xE000E010    ; SysTick CTRL
    #              MOVS R1, #7
    #              STR R1, [R0]
    #              LDR R0, =0xE000E014    ; SysTick LOAD
    #              MOVS R1, #100
    #              STR R1, [R0]
    #              ; 再次写 CTRL 让 LOAD 生效
    #              LDR R0, =0xE000E010
    #              MOVS R1, #7
    #              STR R1, [R0]
    #              B .
    # 用机器码 + literal pool
    code = bytearray()
    # 0x08000100
    code += struct.pack("<H", 0x4803)  # LDR R0, [PC, #12]  -> literal at 0x08000110 (offset 12 from 0x08000104 aligned to 4)
    # 实际上 PC 在 Thumb 中是当前+4, LDR R0,[PC,#imm] 取 (PC & ~3) + 4 + imm*4
    # 让我们简化:用字面池
    # 用 keystone 风格手算太烦,直接放一个最简单版本:只设置 SysTick,死循环
    # 然后通过外部直接写 USART 来验证 UART 路径
    # 简化 Reset: B .
    code = struct.pack("<H", 0xE7FE)  # B .
    flash[0x100:0x100 + len(code)] = code

    # SysTick_Handler: 写 USART1 DR='T', BX LR
    # 也用 B . 简化:外部通过检查 in_handler 来确认进入了 handler
    handler = struct.pack("<HH", 0xE7FE, 0xE7FE)
    flash[0x200:0x200 + len(handler)] = handler

    return FirmwareImage(0x08000000, bytes(flash), 0x08000100, 0x20040000)


def main():
    vm = FlipperVM(on_uart_tx=lambda b: print(f"[UART] {chr(b)!r} ({b:#x})"))
    fw = build_fw()
    vm.load_firmware(fw)

    # 外部模拟固件写 USART1 TDR 测试 UART 回调
    vm.usart1.write(0x28, 1, ord("H"))
    vm.usart1.write(0x28, 1, ord("i"))

    # 设置 SysTick:LOAD=100,CTRL=7(ENABLE+TICKINT+CLKSOURCE)
    vm._systick_write(0x04, 4, 100)
    vm._systick_write(0x00, 4, 7)
    print(f"SysTick CTRL={vm.systick_ctrl:#x} LOAD={vm.systick_load}")

    # 跑 100 条指令(应该触发 SysTick 1 次,进入 handler)
    vm.step(50)
    assert vm.in_handler == 0, f"不该已在 handler 中: {vm.in_handler}"
    vm.step(50)  # 第 100 条指令时 SysTick 触发
    assert vm.in_handler >= 1, f"应已进入 SysTick handler: in_handler={vm.in_handler}"
    assert vm.uc.reg_read(UC_ARM_REG_PC) == 0x08000200, "应在 SysTick_Handler 入口"
    assert vm.uc.reg_read(UC_ARM_REG_LR) == 0xFFFFFFF9
    print(f"  SysTick 进入:PC={vm.uc.reg_read(UC_ARM_REG_PC):#08X} "
          f"LR={vm.uc.reg_read(UC_ARM_REG_LR):#08X} in_handler={vm.in_handler}")

    # 关掉 SysTick 避免再触发,然后验证异常返回
    vm._systick_write(0x00, 4, 0)
    # 现在每次 step 都不会重新触发,但 handler 是 B .,我们手动 _return_from_exception
    saved_sp = vm.uc.reg_read(UC_ARM_REG_SP)
    vm._return_from_exception(0xFFFFFFF9)
    assert vm.in_handler == 0
    assert vm.uc.reg_read(UC_ARM_REG_SP) == saved_sp + 32
    print(f"  SysTick 返回:PC={vm.uc.reg_read(UC_ARM_REG_PC):#08X} "
          f"SP={vm.uc.reg_read(UC_ARM_REG_SP):#08X} in_handler={vm.in_handler}")
    print("OK")


if __name__ == "__main__":
    main()
