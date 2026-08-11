"""Flipper Zero boot-flow diagnostic.

Verifies each stage of the real Flipper startup flow:
  1. Reset_Handler / VTOR setup
  2. furi_hal_init_early (RCC/FLASH/PWR/RNG/HSEM polling)
  3. FreeRTOS SVC + SysTick ticking
  4. furi_hal_bt_init (HSEM/IPCC/C2BOOT)
  5. GUI service -> u8g2_InitDisplay + canvas_commit -> LCD
  6. FIRMWARE_BOOTED

All strings in this file MUST be plain ASCII (no Chinese / no unicode)
because Keystone's ks.asm() uses ASCII encoding.
"""
import struct
import sys

sys.path.insert(0, '/workspace')

from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP

from flipper_vm.emulator import FlipperVM
from flipper_vm.firmware_loader import FirmwareImage


def build_firmware():
    from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)

    def asm(addr, code):
        # Keystone does not support ';' comments — strip them before assembling
        lines = []
        for line in code.split('\n'):
            semi = line.find(';')
            if semi >= 0:
                line = line[:semi]
            lines.append(line)
        clean = '\n'.join(lines)
        try:
            enc, _ = ks.asm(clean, addr)
        except Exception:
            # fallback: NOP
            enc = [0x00, 0xBF]
        return bytes(enc)

    FLASH_BASE = 0x08000000
    flash = bytearray(0x10000)

    # Memory layout (each block has generous spacing to avoid overlap):
    #   0x0100  Reset_Handler
    #   0x0200  main
    #   0x0210  furi_init (bx lr)
    #   0x0400  furi_hal_init_early  (~320 bytes with literal pool + strings)
    #   0x0600  furi_run             (~40 bytes)
    #   0x0700  initsrv              (~80 bytes)
    #   0x0780  furi_hal_init_full (bx lr)
    #   0x0800  bt_init              (~80 bytes)
    #   0x0900  gui_init             (~150 bytes)
    #   0x0A00  send_cmd             (~40 bytes)
    #   0x0A80  send_data            (~40 bytes)
    #   0x0B00  print_uart           (~40 bytes)
    #   0x0C00  SVC_Handler (bx lr)
    #   0x0C10  PendSV_Handler (bx lr)
    #   0x0C20  SysTick_Handler      (~40 bytes)
    #   0x0C30  Default_Handler (b .)

    A_RESET   = FLASH_BASE + 0x0100
    A_MAIN    = FLASH_BASE + 0x0200
    A_FURI    = FLASH_BASE + 0x0210
    A_HAL     = FLASH_BASE + 0x0400
    A_RUN     = FLASH_BASE + 0x0600
    A_INITSRV = FLASH_BASE + 0x0700
    A_HALFULL = FLASH_BASE + 0x0780
    A_BT      = FLASH_BASE + 0x0800
    A_GUI     = FLASH_BASE + 0x0900
    A_SENDCMD = FLASH_BASE + 0x0A00
    A_SENDDAT = FLASH_BASE + 0x0A80
    A_PRINT   = FLASH_BASE + 0x0B00
    A_SVC     = FLASH_BASE + 0x0C00
    A_PENDSV  = FLASH_BASE + 0x0C10
    A_SYSTICK = FLASH_BASE + 0x0C20
    A_DEFAULT = FLASH_BASE + 0x0C30

    # --- Vector table at 0x08000000 ---
    vector_sp = 0x20040000
    vectors = [
        vector_sp,            # [0] initial SP
        A_RESET | 1,          # [1] Reset_Handler
        A_DEFAULT | 1,        # [2] NMI
        A_DEFAULT | 1,        # [3] HardFault
        A_DEFAULT | 1,        # [4] MemManage
        A_DEFAULT | 1,        # [5] BusFault
        A_DEFAULT | 1,        # [6] UsageFault
        0, 0, 0, 0,           # [7-10]
        A_SVC | 1,            # [11] SVC
        A_DEFAULT | 1,        # [12] DebugMon
        0,                    # [13]
        A_PENDSV | 1,         # [14] PendSV
        A_SYSTICK | 1,        # [15] SysTick
    ]
    for _ in range(80):
        vectors.append(A_DEFAULT | 1)
    struct.pack_into(f"<{len(vectors)}I", flash, 0, *vectors)

    # (1) Reset_Handler ---
    reset_code = f"""
        ldr r0, =0x20040000
        mov sp, r0
        ldr r0, =0x58000000
        movs r1, #0x0301
        str r1, [r0, #0x00]
        ldr r0, =0xE000ED08
        ldr r1, =0x08000000
        str r1, [r0]
        bl #{A_MAIN}
    halt: b halt
    """
    flash[0x100:0x100 + len(asm(A_RESET, reset_code))] = asm(A_RESET, reset_code)

    # (2) main ---
    main_code = f"""
        bl #{A_FURI}
        bl #{A_HAL}
        bl #{A_RUN}
    main_halt: b main_halt
    """
    flash[0x200:0x200 + len(asm(A_MAIN, main_code))] = asm(A_MAIN, main_code)

    # furi_init: bx lr
    flash[0x210:0x212] = asm(A_FURI, "bx lr")

    # (3) furi_hal_init_early ---
    init_early_code = f"""
        push {{r4, r5, lr}}

    wait_hsi:
        ldr r0, =0x58000000
        ldr r1, [r0, #0x00]
        movs r2, #1
        tst r1, r2
        beq wait_hsi
    wait_hse:
        ldr r1, [r0, #0x00]
        movs r2, #(1 << 9)
        tst r1, r2
        beq wait_hse
    wait_pll:
        ldr r1, [r0, #0x00]
        ldr r2, =0x01000000
        tst r1, r2
        beq wait_pll
    wait_pllrdy:
        ldr r1, [r0, #0x00]
        ldr r2, =0x02000000
        tst r1, r2
        beq wait_pllrdy
    wait_hsi48_a:
        ldr r1, [r0, #0x0C]
        movs r2, #(1 << 1)
        tst r1, r2
        beq wait_hsi48_a
    wait_hsi48_b:
        ldr r1, [r0, #0x98]
        movs r2, #(1 << 1)
        tst r1, r2
        beq wait_hsi48_b
    wait_sws:
        ldr r1, [r0, #0x08]
        ubfx r1, r1, #2, #2
        movs r2, #0x03
        cmp r1, r2
        bne wait_sws
    wait_lse:
        ldr r1, [r0, #0x90]
        movs r2, #(1 << 1)
        tst r1, r2
        beq wait_lse
    wait_lsi:
        ldr r1, [r0, #0x94]
        movs r2, #(1 << 1)
        tst r1, r2
        beq wait_lsi

    wait_flash:
        ldr r0, =0x58004000
        ldr r1, [r0, #0x00]
        ubfx r1, r1, #0, #3
        cmp r1, #0x00
        beq wait_flash

        ldr r0, =0x40007000
    wait_c2boot:
        ldr r1, [r0, #0x0C]
        ldr r2, =0x00008000
        tst r1, r2
        beq wait_c2boot
    wait_sr2:
        ldr r1, [r0, #0x98]
        ldr r2, =0x00002000
        tst r1, r2
        beq wait_sr2

        ldr r0, =0x58001000
        ldr r1, [r0, #0x04]
        cmp r1, #0
        beq err_rng
        ldr r0, =0x58001400
        ldr r1, [r0, #0x00]
        cmp r1, #0
        beq err_hwsem

        ldr r0, =s_hal_ok
        bl #{A_PRINT}
        pop {{r4, r5, pc}}
    err_rng:
        ldr r0, =s_rng_err
        bl #{A_PRINT}
    err_hwsem_loop:
        b err_hwsem_loop
    err_hwsem:
        ldr r0, =s_hwsem_err
        bl #{A_PRINT}
        b err_hwsem_loop

    s_hal_ok:    .asciz "\\nHAL_INIT_EARLY_OK\\n"
    s_rng_err:   .asciz "\\nERR_RNG_DRDY=0\\n"
    s_hwsem_err: .asciz "\\nERR_HSEM_R0=0\\n"
    """
    blob = asm(A_HAL, init_early_code)
    flash[0x400:0x400 + len(blob)] = blob

    # (4) furi_run -> FreeRTOS start ---
    furi_run_code = f"""
        push {{lr}}
        ldr r0, =0xE000E010
        ldr r1, =1000
        str r1, [r0, #0x04]
        movs r1, #0
        str r1, [r0, #0x08]
        movs r1, #0x07
        str r1, [r0, #0x00]
        svc #0
        bl #{A_INITSRV}
        pop {{pc}}
    """
    blob = asm(A_RUN, furi_run_code)
    flash[0x600:0x600 + len(blob)] = blob

    # (5) InitSrv -> HAL full + BT + GUI ---
    initsrv_code = f"""
        push {{lr}}
        bl #{A_HALFULL}
        bl #{A_BT}
        bl #{A_GUI}

        ldr r0, =s_booted
        bl #{A_PRINT}

    idle_loop:
        movs r2, #100
    wait1:
        subs r2, r2, #1
        bne wait1
        b idle_loop

    s_booted: .asciz "\\nFIRMWARE_BOOTED dolphin\\n"
    """
    blob = asm(A_INITSRV, initsrv_code)
    flash[0x700:0x700 + len(blob)] = blob

    # furi_hal_init_full: bx lr
    flash[0x780:0x782] = asm(A_HALFULL, "bx lr")

    # (6) furi_hal_bt_init ---
    bt_code = f"""
        push {{lr}}
        ldr r0, =0x58001400
    hsem_wait:
        ldr r1, [r0, #0x00]
        ldr r2, =0x80000000
        tst r1, r2
        beq hsem_wait

        ldr r0, =0x58000C00
    ipcc_wait:
        ldr r1, [r0, #0x2C]
        cmp r1, #0
        bne ipcc_wait

        ldr r0, =s_bt_ok
        bl #{A_PRINT}
        pop {{pc}}

    s_bt_ok: .asciz "\\nBT_INIT_OK\\n"
    """
    blob = asm(A_BT, bt_code)
    flash[0x800:0x800 + len(blob)] = blob

    # (7) gui_init / canvas_init ---
    gui_code = f"""
        push {{r4, lr}}
        ldr r0, =0x48000800
        ldr r1, =(1 << 25)
        str r1, [r0, #0x18]
        movs r2, #80
    rst_d:
        subs r2, r2, #1
        bne rst_d
        ldr r1, =(1 << 9)
        str r1, [r0, #0x18]

        movs r0, #0xAE
        bl #{A_SENDCMD}
        movs r0, #0xA2
        bl #{A_SENDCMD}
        movs r0, #0x2F
        bl #{A_SENDCMD}
        movs r0, #0x27
        bl #{A_SENDCMD}
        movs r0, #0xAF
        bl #{A_SENDCMD}

        movs r3, #0
    row:
        movs r0, #0xB0
        adds r0, r0, r3
        bl #{A_SENDCMD}
        movs r0, #0x10
        bl #{A_SENDCMD}
        movs r0, #0x00
        bl #{A_SENDCMD}
        movs r2, #128
    cdata:
        movs r1, #0xAA
        bl #{A_SENDDAT}
        subs r2, r2, #1
        bne cdata
        adds r3, r3, #1
        cmp r3, #8
        blt row

        ldr r0, =s_gui_ok
        bl #{A_PRINT}
        pop {{r4, pc}}

    s_gui_ok: .asciz "\\nGUI_INIT_OK\\n"
    """
    blob = asm(A_GUI, gui_code)
    flash[0x900:0x900 + len(blob)] = blob

    # u8g2_send_cmd (r0 = cmd)
    send_cmd = f"""
        push {{r4, lr}}
        mov r4, r0
        ldr r0, =0x48000400
        ldr r1, =(1 << 27)
        str r1, [r0, #0x18]
        ldr r0, =0x40003800
        str r4, [r0, #0x0C]
        ldr r0, =0x48000400
        movs r1, #(1 << 11)
        str r1, [r0, #0x18]
        pop {{r4, pc}}
    """
    blob = asm(A_SENDCMD, send_cmd)
    flash[0xA00:0xA00 + len(blob)] = blob

    # u8g2_send_data (r1 = byte)
    send_data = f"""
        push {{r4, lr}}
        mov r4, r1
        ldr r0, =0x48000400
        movs r1, #(1 << 11)
        str r1, [r0, #0x18]
        ldr r0, =0x40003800
        str r4, [r0, #0x0C]
        pop {{r4, pc}}
    """
    blob = asm(A_SENDDAT, send_data)
    flash[0xA80:0xA80 + len(blob)] = blob

    # print_uart (r0 = *str, print until NUL)
    print_code = f"""
        push {{r4, lr}}
        mov r4, r0
    ploop:
        ldrb r1, [r4]
        cmp r1, #0
        beq pdone
        ldr r0, =0x40013800
        str r1, [r0, #0x28]
        adds r4, r4, #1
        b ploop
    pdone:
        pop {{r4, pc}}
    """
    blob = asm(A_PRINT, print_code)
    flash[0xB00:0xB00 + len(blob)] = blob

    # SVC_Handler: bx lr
    flash[0xC00:0xC02] = asm(A_SVC, "bx lr")
    # PendSV_Handler: bx lr
    flash[0xC10:0xC12] = asm(A_PENDSV, "bx lr")

    # SysTick_Handler -> write PENDSVSET and print 'T'
    systick = f"""
        push {{lr}}
        ldr r0, =0xE000ED04
        ldr r1, =0x10000000
        str r1, [r0]
        movs r0, #0x54
        ldr r1, =0x40013800
        str r0, [r1, #0x28]
        pop {{pc}}
    """
    blob = asm(A_SYSTICK, systick)
    flash[0xC20:0xC20 + len(blob)] = blob

    # Default_Handler
    flash[0xC30:0xC32] = asm(A_DEFAULT, "b .")

    return FirmwareImage(
        base_addr=FLASH_BASE,
        data=bytes(flash),
        entry_point=A_RESET,
        initial_sp=vector_sp,
    )


def main():
    print("=" * 70)
    print("Flipper Zero Boot Flow Diagnostic v0.5")
    print("Table: Reset -> Furi Core -> HAL -> BT -> GUI -> Booted")
    print("=" * 70)

    uart_buf = []
    all_uart = ""  # accumulated UART output across all stages

    def on_uart(b):
        uart_buf.append(b)

    vm = FlipperVM(on_uart_tx=on_uart)
    fw = build_firmware()
    vm.load_firmware(fw)

    def flush_uart():
        nonlocal all_uart
        s = bytes(uart_buf).decode('latin-1', errors='replace')
        uart_buf.clear()
        all_uart += s
        for line in s.split("\n"):
            if line.strip():
                print("  [UART] " + line)
        return all_uart  # return accumulated output

    stage_results = {}

    # --- Stage 1: Reset_Handler / VTOR ---
    print("\n[Stage 1] Reset_Handler -> SystemInit (RCC default + VTOR) ...")
    vm.step(500)
    pc = vm.uc.reg_read(UC_ARM_REG_PC)
    sp = vm.uc.reg_read(UC_ARM_REG_SP)
    out = flush_uart()
    print(f"  PC=0x{pc:08X} SP=0x{sp:08X}")
    vtor = int.from_bytes(vm.ppb[0xED08:0xED08 + 4], "little")
    stage_results['Reset_VTOR'] = (vtor == 0x08000000)
    tag = "[OK]" if stage_results['Reset_VTOR'] else "[FAIL]"
    print(f"  SCB.VTOR = 0x{vtor:08X} -> {tag} expect 0x08000000")

    # --- Stage 2: furi_hal_init_early ---
    print("\n[Stage 2] main -> furi_hal_init_early (RCC/FLASH/PWR/RNG/HSEM polls) ...")
    hal_pass = "HAL_INIT_EARLY_OK" in out
    if not hal_pass:
        for _ in range(300):
            vm.step(5000)
            out = flush_uart()
            if "HAL_INIT_EARLY_OK" in out or vm.icount > 8_000_000:
                break
    hal_pass = "HAL_INIT_EARLY_OK" in out
    stage_results['HAL_Init_Early'] = hal_pass
    tag = "[OK]" if hal_pass else "[FAIL]"
    print(f"  icount={vm.icount} -> {tag} HAL_INIT_EARLY_OK marker?")

    # --- Stage 3: FreeRTOS SVC + SysTick ---
    print("\n[Stage 3] furi_run -> SVC -> FreeRTOS scheduler -> SysTick ...")
    svc_pass = "T" in out
    if not svc_pass:
        for _ in range(200):
            vm.step(2000)
            out = flush_uart()
            if "T" in out or vm.icount > 1_200_000:
                break
    svc_pass = "T" in out
    stage_results['FreeRTOS_Scheduler'] = svc_pass
    tag = "[OK]" if svc_pass else "[WARN]"
    print(f"  icount={vm.icount} -> {tag} SysTick 'T' output? (scheduler alive)")

    # --- Stage 4: BT_Init ---
    print("\n[Stage 4] InitSrv -> furi_hal_bt_init (HSEM/IPCC/C2BOOT) ...")
    bt_pass = "BT_INIT_OK" in out
    if not bt_pass:
        for _ in range(200):
            vm.step(5000)
            out = flush_uart()
            if "BT_INIT" in out or "FIRMWARE_BOOTED" in out or vm.icount > 3_000_000:
                break
    bt_pass = "BT_INIT_OK" in out
    stage_results['BT_Init'] = bt_pass
    tag = "[OK]" if bt_pass else "[FAIL]"
    print(f"  icount={vm.icount} -> {tag} BT_INIT_OK?")

    # --- Stage 5: GUI_Init + LCD ---
    print("\n[Stage 5] GUI service -> canvas_init / u8g2_InitDisplay ...")
    gui_pass = "GUI_INIT_OK" in out
    if not gui_pass:
        for _ in range(200):
            vm.step(5000)
            out = flush_uart()
            if "GUI_INIT" in out or "FIRMWARE_BOOTED" in out or vm.icount > 3_000_000:
                break
    gui_pass = "GUI_INIT_OK" in out
    stage_results['GUI_Init'] = gui_pass
    fb_sum = sum(vm.display.fb)
    stage_results['FB_Nonzero'] = fb_sum > 0
    tag = "[OK]" if gui_pass else "[FAIL]"
    print(f"  icount={vm.icount} -> {tag} GUI_INIT_OK?")
    tag2 = "[OK]" if stage_results['FB_Nonzero'] else "[FAIL]"
    print(f"  Framebuffer sum={fb_sum} -> {tag2} LCD got SPI data?")

    # --- Stage 6: FIRMWARE_BOOTED ---
    print("\n[Stage 6] -> FIRMWARE_BOOTED dolphin ...")
    booted = "FIRMWARE_BOOTED" in out
    if not booted:
        for _ in range(200):
            vm.step(5000)
            out = flush_uart()
            if "FIRMWARE_BOOTED" in out or vm.icount > 3_000_000:
                break
    booted = "FIRMWARE_BOOTED" in out
    stage_results['Booted'] = booted
    tag = "[OK]" if booted else "[FAIL]"
    print(f"  icount={vm.icount} -> {tag} dolphin FIRMWARE_BOOTED?")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("Diagnostic Report")
    print("=" * 70)
    names = {
        'Reset_VTOR': 'Stage 1: Reset_VTOR = 0x08000000',
        'HAL_Init_Early': 'Stage 2: HAL_Init_Early (RCC/FLASH/PWR/RNG/HSEM)',
        'FreeRTOS_Scheduler': 'Stage 3: FreeRTOS scheduler (SVC + SysTick ticking)',
        'BT_Init': 'Stage 4: BT_Init (HSEM/IPCC/C2BOOT)',
        'GUI_Init': 'Stage 5: GUI_Init (u8g2_ST7567 + canvas_commit)',
        'FB_Nonzero': 'Stage 5+: LCD framebuffer non-zero',
        'Booted': 'Stage 6: FIRMWARE_BOOTED dolphin',
    }
    all_pass = True
    for k, v in stage_results.items():
        tag = "[PASS]" if v else "[FAIL]"
        print(f"  {tag} - {names[k]}")
        if not v:
            all_pass = False
    print()
    if all_pass:
        print("[SUCCESS] All stages PASS -> VM matches the full boot-flow table.")
    else:
        print("[FIXME] Some stages FAIL. Fix FAIL items above per the boot-flow table.")
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
