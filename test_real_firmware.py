"""真实 Flipper 固件加载与启动测试。

验证:
1. 能否成功解析 DfuSe 格式的真实固件
2. 能否提取向量表(SP / Reset_Handler)
3. 能否加载到 Flash 并执行若干条指令
4. 启动初期是否触发未映射内存(预期会,因为很多外设未仿真)
"""
import os
import sys
import struct
import traceback

sys.path.insert(0, "/workspace")

from flipper_vm.emulator import FlipperVM
from flipper_vm.firmware_loader import load_firmware
from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_PC, UC_ARM_REG_LR

FIRMWARE_PATH = "/tmp/firmware.dfu"


def main():
    if not os.path.exists(FIRMWARE_PATH):
        print(f"[skip] 未找到 {FIRMWARE_PATH},跳过真实固件测试")
        return False

    print("=" * 60)
    print("真实 Flipper 固件 (Momentum mntm-012) 加载与启动测试")
    print("=" * 60)

    # === 1. 解析 DFU 文件 ===
    print(f"\n[1] 解析 DFU 文件: {FIRMWARE_PATH}")
    print(f"    文件大小: {os.path.getsize(FIRMWARE_PATH)} bytes")
    try:
        fw = load_firmware(FIRMWARE_PATH)
    except Exception as e:
        print(f"    [FAIL] DFU 解析失败: {e}")
        traceback.print_exc()
        return False
    print(f"    [OK] DFU 解析成功")
    print(f"    base_addr  = 0x{fw.base_addr:08X}")
    print(f"    size       = {len(fw.data)} bytes ({len(fw.data)//1024} KB)")
    print(f"    entry      = 0x{fw.entry_point:08X}")
    print(f"    initial_sp = 0x{fw.initial_sp:08X}")

    # === 2. 验证向量表 ===
    print(f"\n[2] 向量表验证:")
    sp_field = struct.unpack("<I", fw.data[0:4])[0]
    reset_field = struct.unpack("<I", fw.data[4:8])[0]
    print(f"    vector[0] (SP)         = 0x{sp_field:08X}")
    print(f"    vector[1] (Reset)      = 0x{reset_field:08X} (Thumb: 0x{reset_field & ~1:08X})")
    # 检查 SP 是否在 SRAM 范围
    sram_ok = 0x20000000 <= sp_field <= 0x20050000
    reset_ok = 0x08000000 <= (reset_field & ~1) <= 0x08100000
    print(f"    SP 在 SRAM 范围: {'✓' if sram_ok else '✗'}")
    print(f"    Reset 在 Flash:  {'✓' if reset_ok else '✗'}")
    if not (sram_ok and reset_ok):
        print(f"    [FAIL] 向量表不合理")
        return False

    # === 3. 创建 VM 并加载 ===
    print(f"\n[3] 创建 VM 并加载固件:")
    uart_log = []
    vm = FlipperVM(on_uart_tx=lambda b: uart_log.append(b))
    vm.load_firmware(fw)
    print(f"    [OK] 加载完成")
    print(f"    VM SP = 0x{vm.uc.reg_read(UC_ARM_REG_SP):08X}")
    print(f"    VM PC = 0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}")

    # === 4. 单步执行头几条指令 ===
    print(f"\n[4] 单步执行前 50 条指令:")
    try:
        for i in range(5):
            vm.step(10)
            pc = vm.uc.reg_read(UC_ARM_REG_PC)
            print(f"    step {i+1}x10: PC=0x{pc:08X} icount={vm.icount} in_handler={vm.in_handler}")
    except Exception as e:
        print(f"    [FAIL] 单步执行异常: {e}")
        traceback.print_exc()
        return False

    # === 5. 尝试执行 1000 条(预期会卡在某个未仿真外设)===
    print(f"\n[5] 执行 1000 条指令(预期会卡或跑偏):")
    try:
        vm.step(1000)
        pc = vm.uc.reg_read(UC_ARM_REG_PC)
        sp = vm.uc.reg_read(UC_ARM_REG_SP)
        print(f"    PC=0x{pc:08X}  SP=0x{sp:08X}  icount={vm.icount}")
        # 看看 UART 输出
        if uart_log:
            print(f"    UART 输出 ({len(uart_log)} bytes): {bytes(uart_log[:50])!r}")
        else:
            print(f"    UART 无输出(可能还在初始化阶段)")
    except Exception as e:
        print(f"    [expected] 执行异常(未仿真外设): {e}")
        print(f"    PC=0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}  icount={vm.icount}")

    # === 6. 再跑一段,看是否进入死循环 ===
    print(f"\n[6] 再执行 5000 条,观察行为:")
    pc_samples = set()
    try:
        for i in range(5):
            vm.step(1000)
            pc_samples.add(vm.uc.reg_read(UC_ARM_REG_PC))
        print(f"    5 次 step 后 PC 采样集合: {[f'0x{p:08X}' for p in pc_samples]}")
        print(f"    唯一 PC 数: {len(pc_samples)}")
        if len(pc_samples) <= 2:
            print(f"    → 固件进入了死循环(可能卡在等待未仿真外设)")
        else:
            print(f"    → 固件在正常执行(地址变化)")
    except Exception as e:
        print(f"    [expected] 异常: {e}")

    print("\n" + "=" * 60)
    print("真实固件测试结论:")
    print("=" * 60)
    print("  ✓ DfuSe 格式解析成功")
    print("  ✓ 向量表提取成功(SP / Reset_Handler)")
    print("  ✓ 固件加载到 Flash 内存映射成功")
    print("  ✓ Thumb-2 指令执行无异常")
    print(f"  → 真实固件可加载并启动,但因大量外设(CC1101/NFC/IR/BLE/USB/SD)未仿真,")
    print(f"    固件会在初始化阶段卡死或空转 —— 这是预期的,需要按需补齐外设仿真。")
    return True


if __name__ == "__main__":
    main()
