"""固件加载器:支持 Flipper Zero 的 .bin 与 .dfu(DfuSe)格式."""
import struct
from dataclasses import dataclass
from typing import List


@dataclass
class FirmwareImage:
    base_addr: int          # 加载到 Flash 中的物理地址
    data: bytes
    entry_point: int        # Reset_Handler 地址(向量表第 2 项)
    initial_sp: int         # 向量表第 1 项


def load_firmware(path: str) -> FirmwareImage:
    with open(path, "rb") as f:
        blob = f.read()

    if blob[:5] == b"DfuSe":
        images = _parse_dfuse(blob)
    else:
        # 原始 bin:整段直接加载到 Flash 基址
        images = [(0x08000000, blob)]

    if not images:
        raise ValueError("固件中未找到任何可加载镜像")

    # 找到最低地址的镜像(向量表所在),拼接为完整 Flash 块
    images.sort(key=lambda x: x[0])
    base = images[0][0]
    end = max(addr + len(data) for addr, data in images)
    size = end - base
    flash = bytearray(size)
    for addr, data in images:
        off = addr - base
        flash[off:off + len(data)] = data

    # 解析向量表(SP + Reset)
    if len(flash) < 8:
        raise ValueError("镜像太小,无法解析向量表")
    initial_sp, reset_handler = struct.unpack_from("<II", flash, 0)
    return FirmwareImage(base_addr=base, data=bytes(flash),
                         entry_point=reset_handler, initial_sp=initial_sp)


def _parse_dfuse(blob: bytes):
    """解析 DfuSe 文件,返回 [(addr, data), ...]."""
    # DfuSe 前缀:5 'DfuSe' + 1 ver + 4 size + 1 n_targets = 11 字节
    if len(blob) < 11:
        raise ValueError("DfuSe 文件过短")
    prefix, ver, img_size, n_targets = struct.unpack_from("<5sBIB", blob, 0)
    if prefix != b"DfuSe" or ver != 0x01:
        raise ValueError(f"非 DfuSe 文件:prefix={prefix!r} ver={ver}")

    images = []
    off = 11
    for _ in range(n_targets):
        # Target prefix:6 'Target' + 1 alt + 4 named + 255 name + 4 size + 4 n_elems = 274
        if off + 274 > len(blob):
            raise ValueError("Target 前缀不完整")
        sig = blob[off:off + 6]
        if sig != b"Target":
            raise ValueError(f"Target 签名错误:{sig!r}")
        n_elems = struct.unpack_from("<I", blob, off + 270)[0]
        off += 274
        for _ in range(n_elems):
            if off + 8 > len(blob):
                raise ValueError("Element 头不完整")
            elem_addr, elem_size = struct.unpack_from("<II", blob, off)
            off += 8
            if off + elem_size > len(blob):
                raise ValueError("Element 数据越界")
            images.append((elem_addr, blob[off:off + elem_size]))
            off += elem_size
    return images
