"""冒烟测试:验证 FlipperVM 能加载并执行最小 Thumb-2 镜像。"""
import struct

from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_PC

from flipper_vm.emulator import FlipperVM
from flipper_vm.firmware_loader import FirmwareImage


def make_minimal_fw():
    """构造一个最小镜像:
       vector[0] = SP = 0x20040000
       vector[1] = Reset_Handler = 0x08000101 (Thumb)
       0x08000100: 0xE7FE  =  B .   (无限循环)
    """
    flash = bytearray(0x200)
    struct.pack_into("<II", flash, 0, 0x20040000, 0x08000101)
    # Reset_Handler 地址 + Thumb 标志
    struct.pack_into("<H", flash, 0x100, 0xE7FE)
    return FirmwareImage(base_addr=0x08000000, data=bytes(flash),
                         entry_point=0x08000100, initial_sp=0x20040000)


def main():
    vm = FlipperVM()
    fw = make_minimal_fw()
    vm.load_firmware(fw)

    print(f"SP  = 0x{vm.uc.reg_read(UC_ARM_REG_SP):08X}")
    print(f"PC  = 0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}")

    vm.step(5)
    print(f"after 5 steps:")
    print(f"  SP  = 0x{vm.uc.reg_read(UC_ARM_REG_SP):08X}")
    print(f"  PC  = 0x{vm.uc.reg_read(UC_ARM_REG_PC):08X}")
    print(f"  icount = {vm.icount}")

    # 按下 back 按键,看 GPIO PC0 IDR 是否变 0
    vm.set_button("back", True)
    idr = vm.gpioc.read(0x10, 4)
    print(f"  PC IDR (back pressed) = 0x{idr:08X}")
    vm.set_button("back", False)
    idr = vm.gpioc.read(0x10, 4)
    print(f"  PC IDR (back released)= 0x{idr:08X}")

    # 测试 SPI2 -> 显示数据流:DC=1,写 8 个字节,看 framebuffer 是否变化
    vm.gpiob.write(0x14, 4, 1 << 11)   # ODR.PB11 = 1 -> DC=1(data)
    before = sum(vm.display.fb)
    for b in (0xFF, 0x00, 0xAA, 0x55, 0xFF, 0x00, 0xAA, 0x55):
        vm.spi2.write(0x0C, 1, b)
    after = sum(vm.display.fb)
    print(f"  SPI2 writes changed framebuffer sum: {before} -> {after}")
    print("OK")


if __name__ == "__main__":
    main()
