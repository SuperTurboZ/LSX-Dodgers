"""Build the MZ-2000 port and verify both media images as binary data."""

import os
import struct
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DISK_DIR = ROOT / "disk"
SECTOR_SIZE = 512
TRACKS = 80
SECTORS_PER_TRACK = 9
TOTAL_SECTORS = TRACKS * SECTORS_PER_TRACK
D88_HEADER = 0x2B0
FAT_SIZE = 2
ROOT_ENTRIES = 64
ROOT_LBA = 1 + 2 * FAT_SIZE
DATA_LBA = ROOT_LBA + ROOT_ENTRIES * 32 // SECTOR_SIZE
IPL_LBA = 9
MZT_LBA = 10
MZT_FIRST_CLUSTER = MZT_LBA - DATA_LBA + 2
MEDIA = 0xFD
FILENAME = b"LDMZ2000MZT"
INVERT = bytes(reversed(range(256)))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def words(data, offset, count):
    return struct.unpack_from(f"<{count}H", data, offset)


def fat_get(fat, cluster):
    value = words(fat, cluster * 3 // 2, 1)[0]
    return value >> 4 if cluster & 1 else value & 0xFFF


def fat_set(fat, cluster, value):
    offset = cluster * 3 // 2
    old = words(fat, offset, 1)[0]
    new = (old & 0xF) | (value << 4) if cluster & 1 else (old & 0xF000) | value
    struct.pack_into("<H", fat, offset, new)


def sector_count(size):
    return (size + SECTOR_SIZE - 1) // SECTOR_SIZE


def boot_sector():
    sector = bytearray(SECTOR_SIZE)
    sector[:11] = b"\xeb\x3c\x90M2K FAT "
    struct.pack_into(
        "<HBHBHHBHHHII", sector, 11, SECTOR_SIZE, 1, 1, 2, ROOT_ENTRIES,
        TOTAL_SECTORS, MEDIA, FAT_SIZE, SECTORS_PER_TRACK, 2, 0, 0,
    )
    sector[38] = 0x29
    sector[43:62] = b"LSX MZ-2000FAT12   "
    sector[-2:] = b"\x55\xaa"
    return sector


def build_d88(ipl, mzt):
    require(len(ipl) == 256, "IPL sector must be exactly 256 bytes")
    count = sector_count(len(mzt))
    require(count and MZT_LBA + count <= TOTAL_SECTORS, "MZT does not fit on disk")
    fat = bytearray(FAT_SIZE * SECTOR_SIZE)
    fat[:3] = bytes((MEDIA, 0xFF, 0xFF))
    fat_set(fat, IPL_LBA - DATA_LBA + 2, 0xFF7)
    for cluster in range(MZT_FIRST_CLUSTER, MZT_FIRST_CLUSTER + count):
        last = cluster == MZT_FIRST_CLUSTER + count - 1
        fat_set(fat, cluster, 0xFFF if last else cluster + 1)

    sectors = [bytes(SECTOR_SIZE)] * TOTAL_SECTORS
    sectors[0] = boot_sector()
    for start in (1, 1 + FAT_SIZE):
        for i in range(FAT_SIZE):
            sectors[start + i] = fat[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE]
    directory = bytearray(SECTOR_SIZE)
    directory[:11] = FILENAME
    directory[11] = 0x07  # read-only, hidden, system
    struct.pack_into("<HI", directory, 26, MZT_FIRST_CLUSTER, len(mzt))
    sectors[ROOT_LBA] = directory
    sectors[IPL_LBA] = ipl.translate(INVERT)
    for i in range(count):
        sectors[MZT_LBA + i] = mzt[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE].ljust(
            SECTOR_SIZE, b"\0"
        )

    image = bytearray(D88_HEADER)
    image[:11] = b"LSX MZ-2000"
    for track in range(TRACKS):
        struct.pack_into("<I", image, 0x20 + track * 4, len(image))
        for record in range(1, SECTORS_PER_TRACK + 1):
            lba = track * SECTORS_PER_TRACK + record - 1
            sector = sectors[lba]
            image.extend(struct.pack(
                "<BBBBH8xH", track // 2, track % 2, record,
                1 if lba == IPL_LBA else 2, SECTORS_PER_TRACK, len(sector),
            ))
            image.extend(sector)
    struct.pack_into("<I", image, 0x1C, len(image))
    return image


def verify_d88(image, ipl, mzt):
    require(struct.unpack_from("<I", image, 0x1C)[0] == len(image), "D88 size")
    require(not any(image[0x20 + TRACKS * 4:D88_HEADER]), "D88 extra tracks")
    sectors = []
    offset = D88_HEADER
    for track in range(TRACKS):
        mapped = struct.unpack_from("<I", image, 0x20 + track * 4)[0]
        require(mapped == offset, f"D88 track offset {track}")
        for number in range(1, SECTORS_PER_TRACK + 1):
            lba = len(sectors)
            n, size = (1, 256) if lba == IPL_LBA else (2, SECTOR_SIZE)
            header = struct.unpack_from("<BBBBH8xH", image, offset)
            require(header == (track // 2, track % 2, number, n, 9, size),
                    f"D88 sector header LBA {lba}")
            require(not any(image[offset + 6:offset + 14]), f"D88 sector flags {lba}")
            offset += 16
            sectors.append(image[offset:offset + size])
            offset += size
    require(offset == len(image), "D88 trailing or missing data")
    require(sectors[IPL_LBA].translate(INVERT) == ipl, "IPL inversion")

    bpb = sectors[0]
    require(struct.unpack_from("<HBHBHHBHHH", bpb, 11) ==
            (512, 1, 1, 2, 64, 720, MEDIA, 2, 9, 2), "FAT12 BPB")
    require(bpb[-2:] == b"\x55\xaa", "FAT12 signature")
    fat = b"".join(sectors[1:ROOT_LBA - FAT_SIZE])
    require(fat == b"".join(sectors[ROOT_LBA - FAT_SIZE:ROOT_LBA]), "FAT copies")
    require(fat_get(fat, 2) == 0xFF7, "IPL cluster reservation")
    entry = sectors[ROOT_LBA][:32]
    start_cluster, length = struct.unpack_from("<HI", entry, 26)
    require(entry[:11] == FILENAME and length == len(mzt), "MZT root entry")
    require(start_cluster == MZT_FIRST_CLUSTER, "MZT start cluster")
    count = sector_count(length)
    for index in range(count):
        cluster = start_cluster + index
        expected = 0xFFF if index == count - 1 else cluster + 1
        require(fat_get(fat, cluster) == expected, f"MZT chain at cluster {cluster}")
    data = b"".join(sectors[MZT_LBA:MZT_LBA + count])
    require(data == mzt.ljust(count * SECTOR_SIZE, b"\0"), "MZT embedded contents")


def read_symbols(stem):
    symbols = {}
    for line in (ROOT / f"{stem}.SYM").read_text(encoding="utf-8-sig").splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                symbols[fields[1].upper()] = int(fields[0], 16)
            except ValueError:
                continue
    return symbols


def verify_body(body, symbols):
    require(body[0] == 1 and body[128] == 0xC3, "LD body header or entry opcode")
    require(words(body, 18, 3) == (len(body) - 128, 0x8080, 0x8080), "LD header")
    require(symbols["OFFSET"] + len(body) == symbols["LAST_ADR"], "LD image size")
    require(symbols["LAST_ADR"] <= 0x10000, "LD image crosses FFFFh")
    require(symbols["WORKAD"] == symbols["CPM_BOOT"] == 0xF000, "BIOS address")
    require(symbols["STABLE"] == 0xF100 and symbols["_DEVICE"] == 0xF200,
            "BDOS table or DPB address")
    require(symbols["RESIDENT_END"] == 0xF300, "resident image overlaps work buffers")
    require(symbols["RESIDENT_END"] - symbols["BDOS"] ==
            symbols["LAST_ADR"] - symbols["INITE"], "resident relocation size")
    require(symbols["M2K_START"] < symbols["INITE"] < symbols["BDOS"],
            "resident copy would overlap the running initializer")
    require(symbols["BDOS"] <= symbols["M2K_MAP"] <
            symbols["M2K_GRAM_SAFE_END"] <= 0xC000, "GRAM mapping region")
    require(symbols["M2K_GRAM_STACK_END"] - symbols["M2K_GRAM_STACK"] == 32,
            "bank stack bounds")
    require(symbols["M2K_GRAM_STACK"] - symbols["M2K_GRAM_BUFFER"] == 256,
            "GRAM bounce buffer bounds")
    require(symbols["M2K_VIDEO_BEGIN"] < symbols["M2K_TVRAM_SAFE_END"] <= 0xD000,
            "TVRAM code overlaps mapped window")


def verify_mzt(body, mzt, symbols):
    require(mzt[0] == 1 and mzt[128] == 0xF3, "outer MZT mode or entry")
    require(words(mzt, 18, 3) == (len(mzt) - 128, 0x1200, 0x1200), "outer MZT header")
    require(symbols["MZT_HEADER"] == 0x1180 and symbols["MZT_ENTRY"] == 0x1200,
            "outer MZT load and entry symbols")
    require(symbols["BODY_DEST"] == 0x8000 and symbols["BODY_ENTRY"] == 0x8080,
            "inner MZT relocation symbols")
    require(symbols["MZT_HEADER"] + sector_count(len(mzt)) * SECTOR_SIZE <= 0x8000,
            "sector-rounded MZT load overlaps final body")
    require(symbols["BODY_SIZE"] == len(body) and
            mzt[symbols["BODY_SRC"] - symbols["MZT_HEADER"]:] == body,
            "inner LD image differs")


def verify_ipl(ipl, symbols, mzt):
    entry = symbols["IPL_ENTRY"]
    require(len(ipl) == 256 and ipl[:7] == b"\x01IPLPRO", "IPL size or model")
    require(ipl[0x12:0x14] == bytes((0x18, entry - 0x14)), "IPL reset jump")
    require(words(ipl, 0x14, 6) == (256, 0, entry, 0, 0, 0), "IPL header")
    require(0x20 <= entry < symbols["IPL_END"] <= 256, "IPL code addresses")
    require(symbols["MZT_LBA"] == MZT_LBA and
            symbols["MZT_SECTORS"] == sector_count(len(mzt)) and
            symbols["MZT_PAYLOAD_BYTES"] == len(mzt) - 128,
            "IPL embedded MZT layout")
    require(symbols["IPL_END"] < symbols["BOOT_STACK"] == symbols["MZT_HEADER_RAM"],
            "IPL stack or sector staging address")
    require(symbols["MZT_HEADER_RAM"] + 128 == symbols["MZT_PAYLOAD_RAM"] == 0x1200,
            "IPL sector staging does not place the entry at 1200h")
    require(symbols["BOOT_TRACK"] * SECTORS_PER_TRACK + symbols["BOOT_SECTOR"] - 1
            == MZT_LBA, "IPL start track and sector")


def assemble(assembler, stem, output, definitions=()):
    command = [
        assembler, "-i", f"{stem}.ASM", "-bin", output,
        "-sym", f"{stem}.SYM", "-lst", f"{stem}.LST", "-f", "-gap", "0",
    ]
    if definitions:
        command.extend(("-dl", *definitions))
    subprocess.run(command, cwd=ROOT, check=True)
    return (ROOT / output).read_bytes(), read_symbols(stem)


def main():
    assembler = os.environ.get("AILZ80ASM") or "AILZ80ASM.exe"
    body, symbols = assemble(assembler, "M2K", "M2K.BIN")
    verify_body(body, symbols)
    mzt_path = DISK_DIR / "ldmz2000.mzt"
    mzt, symbols = assemble(assembler, "M2KMZT", str(mzt_path.relative_to(ROOT)))
    verify_mzt(body, mzt, symbols)
    ipl, symbols = assemble(assembler, "M2KIPL", "M2KIPL.BIN", (
        f"MZT_LBA={MZT_LBA}", f"MZT_SECTORS={sector_count(len(mzt))}",
        f"MZT_PAYLOAD_BYTES={len(mzt) - 128}",
    ))
    verify_ipl(ipl, symbols, mzt)
    target = DISK_DIR / "ldmz2000.d88"
    target.write_bytes(build_d88(ipl, mzt))
    verify_d88(target.read_bytes(), ipl, mzt)
    print(f"Verified disk/{mzt_path.name} ({len(mzt)} bytes) and "
          f"disk/{target.name} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
