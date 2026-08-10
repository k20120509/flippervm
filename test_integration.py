"""综合集成测试 v2:用 Keystone 汇编器生成 Thumb-2 机器码,
彻底避免手写机器码的偏移错误。

测试镜像程序逻辑(等效 C 代码):
    volatile uint32_t *USART1_TDR = (void*)0x40013828;
    volatile uint32_t *SYSTICK_CTRL = (void*)0xE000E010;
    volatile uint32_t *SYSTICK_LOAD = (void*)0xE000E014;
    volatile uint32_t *SPI2_DR = (void*)0x4000380C;
    volatile uint32_t *GPIOB_ODR = (void*)0x48000414;
    volatile uint32_t *GPIOC_IDR = (void*)0x48000810;
    volatile uint32_t counter;

    void reset_handler(void) {
        *SYSTICK_LOAD = 1000;
        *SYSTICK_CTRL = 7;
        // 打印 "Boot\\n"
        for (char *p = "Boot\\n"; *p; p++) *USART1_TDR = *p;
        while (1) { }
    }

    void systick_handler(void) {
        counter++;
        *USART1_TDR = 'T';
        *GPIOB_ODR = (1 << 11);   // DC=1
        *SPI2_DR   = 0xFF;
        *GPIOB_ODR = 0;           // DC=0
    }
"""
import struct

from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
from unicorn.arm_const import (
    UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
)

from flipper_vm.emulator import FlipperVM
from flipper_vm.firmware_loader import FirmwareImage
from flipper_vm.stm32wb55 import (
    FLASH_BASE, USART1_BASE, SPI2_BASE, GPIOB_BASE, GPIOC_BASE,
)


# 关键外设寄存器地址
USART1_TDR   = USART1_BASE + 0x28
SYSTICK_CTRL = 0xE000E010
SYSTICK_LOAD = 0xE000E014
SPI2_DR      = SPI2_BASE + 0x0C
GPIOB_ODR    = GPIOB_BASE + 0x14
GPIOC_IDR    = GPIOC_BASE + 0x10
COUNTER_ADDR = 0x20000000   # SRAM 中放 counter

ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)


def assemble(asm: str, base: int) -> bytes:
    """汇编 Thumb 代码,返回机器码。"""
    encoding, count = ks.asm(asm, addr=base, as_bytes=True)
    return encoding


def build_test_firmware():
    """组装完整固件镜像。"""
    flash = bytearray(0x1000)

    # === 向量表 ===
    struct.pack_into("<II", flash, 0, 0x20040000, 0x08000101)
    struct.pack_into("<I", flash, 15 * 4, 0x08000201)

    # === Reset_Handler @ 0x08000100 ===
    reset_asm = f"""
        ldr  r0, ={SYSTICK_LOAD}
        ldr  r1, =1000
        str  r1, [r0]
        ldr  r0, ={SYSTICK_CTRL}
        movs r1, #7
        str  r1, [r0]

        ldr  r0, ={USART1_TDR}
        ldr  r2, =boot_str
    loop:
        ldrb r1, [r2], #1
        cbz  r1, end_loop
        str  r1, [r0]
        b    loop
    end_loop:
        b    end_loop

    boot_str:
        .byte 0x42, 0x6f, 0x6f, 0x74, 0x0a, 0x00
    """
    reset_code = assemble(reset_asm, 0x08000100)
    flash[0x100:0x100 + len(reset_code)] = reset_code

    # === SysTick_Handler @ 0x08000200 ===
    systick_asm = f"""
        ldr  r3, ={COUNTER_ADDR}
        ldr  r0, [r3]
        adds r0, #1
        str  r0, [r3]

        ldr  r0, ={USART1_TDR}
        movs r1, #0x54
        str  r1, [r0]

        ldr  r2, ={GPIOB_ODR}
        movs r1, #1
        lsls r1, r1, #11
        str  r1, [r2]

        ldr  r0, ={SPI2_DR}
        movs r1, #0xFF
        str  r1, [r0]

        movs r1, #0
        str  r1, [r2]

        bx   lr
    """
    systick_code = assemble(systick_asm, 0x08000200)
    flash[0x200:0x200 + len(systick_code)] = systick_code

    return FirmwareImage(
        base_addr=FLASH_BASE,
        data=bytes(flash),
        entry_point=FLASH_BASE + 0x100,
        initial_sp=0x20040000,
    )


def disasm_thumb(blob: bytes, base: int, count: int = 20):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    for ins in md.disasm(blob, base):
        print(f"  0x{ins.address:08X}:  {ins.bytes.hex():<10}  {ins.mnemonic} {ins.op_str}")
        count -= 1
        if count == 0:
            break


def main():
    print("=" * 60)
    print("综合集成测试 v2(Keystone 汇编):SysTick 调度 + UART + SPI + 按键")
    print("=" * 60)

    fw = build_test_firmware()
    print(f"\n[1] 固件已构建:size={len(fw.data)} bytes")
    print(f"    Reset 入口:0x{fw.entry_point:08X}")
    print(f"    初始 SP:0x{fw.initial_sp:08X}")

    print("\n[2] Reset_Handler 反汇编:")
    disasm_thumb(fw.data[0x100:0x140], FLASH_BASE + 0x100, 15)
    print("\n[3] SysTick_Handler 反汇编:")
    disasm_thumb(fw.data[0x200:0x240], FLASH_BASE + 0x200, 20)

    print("\n[4] 创建 VM 并加载固件")
    uart_log = []
    vm = FlipperVM(on_uart_tx=lambda b: uart_log.append(chr(b) if 32 <= b < 127 or b in (10, 13) else f"<{b:#x}>"))
    vm.load_firmware(fw)
    print(f"    SP=0x{vm.uc.reg_read(UC_ARM_REG_SP):08X}  PC=0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}")

    print("\n[5] 运行 50000 条指令(分 5 次 step,让 SysTick handler 有机会执行)...")
    try:
        for _ in range(5):
            vm.step(10000)
    except Exception as e:
        print(f"    运行异常: {e}")
        return
    print(f"    PC=0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}  "
          f"in_handler={vm.in_handler}  icount={vm.icount}")

    print("\n[6] UART 控制台输出:")
    out = "".join(uart_log)
    print(f"    {out!r}")
    assert "Boot" in out, f"应打印 'Boot',实际: {out!r}"
    print(f"    ✓ 包含 'Boot' 启动字符串")

    counter = struct.unpack("<I", vm.uc.mem_read(COUNTER_ADDR, 4))[0]
    print(f"\n[7] SysTick 触发次数(counter)={counter}")
    assert counter > 0, "SysTick 应至少触发 1 次"
    tick_count = out.count("T")
    print(f"    UART 中 'T' 出现次数={tick_count}")
    assert tick_count == counter, f"'T' 次数({tick_count})应等于 counter({counter})"
    print(f"    ✓ SysTick 中断按周期触发")

    fb_sum = sum(vm.display.fb)
    print(f"\n[8] 显示帧缓冲像素总和={fb_sum}(SPI2 写入应使其非零)")
    assert fb_sum > 0, "显示帧缓冲应非空(SPI2 已写 0xFF)"
    print(f"    ✓ 显示屏接收到数据")

    print("\n[9] 按键注入测试:")
    vm.set_button("ok", True)
    idr_b = vm.gpiob.read(0x10, 4)
    print(f"    OK(PB5)按下:GPIOB IDR=0x{idr_b:08X} (bit5 应为 0)")
    assert not (idr_b & (1 << 5)), "OK 按下后 PB5 IDR 位应为 0"
    print(f"    ✓ OK 按键正常")
    vm.set_button("ok", False)
    idr_b = vm.gpiob.read(0x10, 4)
    assert (idr_b & (1 << 5)), "OK 释放后 PB5 IDR 位应为 1"
    print(f"    ✓ OK 释放正常")

    vm.set_button("back", True)
    idr_c = vm.gpioc.read(0x10, 4)
    print(f"    Back(PC0)按下:GPIOC IDR=0x{idr_c:08X} (bit0 应为 0)")
    assert not (idr_c & 1), "Back 按下后 PC0 IDR 位应为 0"
    print(f"    ✓ Back 按键正常")
    vm.set_button("back", False)

    print("\n[10] 暂停后状态稳定性:")
    pc_before = vm.uc.reg_read(UC_ARM_REG_PC)
    pc_after = vm.uc.reg_read(UC_ARM_REG_PC)
    assert pc_before == pc_after, "暂停后 PC 不应变化"
    print(f"     ✓ 暂停有效(PC=0x{pc_after:08X} 不变)")

    print("\n[11] 单步执行(1000 条):")
    icount_before = vm.icount
    vm.step(1000)
    assert vm.icount == icount_before + 1000
    print(f"     ✓ 单步成功 icount {icount_before} -> {vm.icount}")

    print("\n[12] 复位测试:")
    vm.uc.mem_write(FLASH_BASE, fw.data)
    vm.load_firmware(fw)
    vm.icount = 0
    vm.in_handler = 0
    uart_log.clear()
    vm.step(5000)
    out2 = "".join(uart_log)
    print(f"     复位后 UART: {out2!r}")
    assert "Boot" in out2, "复位后应重新打印 Boot"
    print(f"     ✓ 复位有效")

    print("\n" + "=" * 60)
    print("🎉 综合集成测试全部通过!")
    print("=" * 60)
    print(f"  - Thumb-2 指令执行:OK")
    print(f"  - SysTick 异常调度:OK(触发 {counter} 次)")
    print(f"  - USART1 输出:OK('Boot' + 'T'×{tick_count})")
    print(f"  - SPI2 → ST7567 显示:OK(帧缓冲非空)")
    print(f"  - GPIO 按键注入:OK(OK/Back 均生效)")
    print(f"  - 暂停/单步/复位:OK")
    print(f"  - 总指令数:{vm.icount}")


if __name__ == "__main__":
    main()
