"""Build the MZ-2000 port and verify both media images as binary data."""

import os
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent
DISK_DIR = ROOT / "disk"
SECTOR_SIZE = 512
TRACKS = 80
SECTORS_PER_TRACK = 9
TOTAL_SECTORS = TRACKS * SECTORS_PER_TRACK
D88_HEADER = 0x2B0
FAT_SIZE = 3
ROOT_ENTRIES = 32
ROOT_LBA = 1 + 2 * FAT_SIZE
DATA_LBA = ROOT_LBA + ROOT_ENTRIES * 32 // SECTOR_SIZE
IPL_LBA = 9
MZT_LBA = 10
MZT_FIRST_CLUSTER = MZT_LBA - DATA_LBA + 2
MEDIA = 0xFD
FILENAME = b"LDMZ2000MZT"
COMMANDS = (
    "FORMAT", "MKDIR", "RMDIR", "TYPE", "PWD", "PAUSE", "BPBINFO",
    "UNZIPFIX", "PMEXTLD", "FAKEFREE",
)
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


def build_d88(ipl, mzt, commands=()):
    require(len(ipl) == 256, "IPL sector must be exactly 256 bytes")
    require(DATA_LBA <= IPL_LBA < MZT_LBA, "IPL must occupy its own data cluster")
    require((TOTAL_SECTORS - DATA_LBA + 2) * 3 <= FAT_SIZE * SECTOR_SIZE * 2,
            "FAT12 does not cover every data cluster")
    files = [(FILENAME, mzt, 0x07), *commands]
    require(len(files) <= ROOT_ENTRIES, "too many D88 root entries")
    fat = bytearray(FAT_SIZE * SECTOR_SIZE)
    fat[:3] = bytes((MEDIA, 0xFF, 0xFF))
    fat_set(fat, IPL_LBA - DATA_LBA + 2, 0xFF7)

    sectors = [bytes(SECTOR_SIZE)] * TOTAL_SECTORS
    sectors[0] = boot_sector()
    sectors[IPL_LBA] = ipl.translate(INVERT)
    directory = bytearray(ROOT_ENTRIES * 32)
    lba = MZT_LBA
    for index, (name, content, attributes) in enumerate(files):
        count = sector_count(len(content))
        require(len(name) == 11 and count and lba + count <= TOTAL_SECTORS,
                f"invalid FAT12 name or file size: {name!r}")
        start_cluster = lba - DATA_LBA + 2
        entry = index * 32
        directory[entry:entry + 11] = name
        directory[entry + 11] = attributes
        struct.pack_into("<HI", directory, entry + 26, start_cluster, len(content))
        for i in range(count):
            cluster = start_cluster + i
            fat_set(fat, cluster, 0xFFF if i == count - 1 else cluster + 1)
            sectors[lba + i] = content[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE].ljust(
                SECTOR_SIZE, b"\0")
        lba += count

    for start in (1, 1 + FAT_SIZE):
        for i in range(FAT_SIZE):
            sectors[start + i] = fat[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE]
    for lba in range(ROOT_LBA, DATA_LBA):
        offset = (lba - ROOT_LBA) * SECTOR_SIZE
        sectors[lba] = directory[offset:offset + SECTOR_SIZE]

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


def verify_d88(image, ipl, mzt, commands=()):
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
            (SECTOR_SIZE, 1, 1, 2, ROOT_ENTRIES, TOTAL_SECTORS, MEDIA,
             FAT_SIZE, SECTORS_PER_TRACK, 2), "FAT12 BPB")
    require(bpb[-2:] == b"\x55\xaa", "FAT12 signature")
    fat = b"".join(sectors[1:ROOT_LBA - FAT_SIZE])
    require(fat == b"".join(sectors[ROOT_LBA - FAT_SIZE:ROOT_LBA]), "FAT copies")
    require(fat_get(fat, IPL_LBA - DATA_LBA + 2) == 0xFF7, "IPL cluster reservation")
    directory = b"".join(sectors[ROOT_LBA:DATA_LBA])
    files = [(FILENAME, mzt, 0x07), *commands]
    next_cluster = MZT_FIRST_CLUSTER
    for index, (name, content, attributes) in enumerate(files):
        entry = directory[index * 32:(index + 1) * 32]
        start_cluster, length = struct.unpack_from("<HI", entry, 26)
        require(entry[:11] == name and entry[11] == attributes and length == len(content),
                f"FAT12 root entry for {name!r}")
        require(start_cluster == next_cluster, f"FAT12 start cluster for {name!r}")
        count = sector_count(length)
        next_cluster += count
        for cluster in range(start_cluster, next_cluster):
            expected = 0xFFF if cluster == next_cluster - 1 else cluster + 1
            require(fat_get(fat, cluster) == expected, f"FAT12 chain at cluster {cluster}")
        lba = DATA_LBA + start_cluster - 2
        data = b"".join(sectors[lba:lba + count])
        require(data == content.ljust(count * SECTOR_SIZE, b"\0"),
                f"FAT12 embedded contents for {name!r}")
    require(not any(directory[len(files) * 32:]), "unexpected FAT12 root entries")


def read_symbols(path):
    symbols = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                symbols[fields[1].upper()] = int(fields[0], 16)
            except ValueError:
                continue
    return symbols


def choose_bdos_page(symbols):
    current_page = symbols["BDOS"] & 0xFF00
    highest = (current_page + symbols["WORKAD"] - symbols["AT_WORK"]) & ~0xFF
    for page in range(highest, symbols["INITE"] - 6, -0x100):
        shift = page - current_page
        if (symbols["M2K_TVRAM_SAFE_END"] + shift <= 0xD000 or
                symbols["M2K_TVRAM_SAFE_BEGIN"] + shift >= 0xE000):
            return page
    raise ValueError("no DOS page fits the fixed work area and the TVRAM window")


def verify_ram_disk(body, symbols, drive, dpb_label, disk, readers, name):
    dpb = symbols[dpb_label]
    require(dpb == symbols["_DEVICE"] + 32 * (ord(drive) - ord("A")),
            f"{drive}: drive table position")
    offset = symbols["INITE"] - symbols["OFFSET"] + dpb - symbols["BDOS"]
    entry = body[offset:offset + 32]
    require(words(entry, 0, 3) == (0, *(symbols[label] for label in readers)),
            f"{drive}: probe default or transfer vectors")
    require(entry[7] == 1 and entry[15] == 2 and entry[28:32] == name,
            f"{drive}: cluster size, sector size or name")
    fat_sectors = symbols[f"M2K_{disk}_FAT_SECTORS"]
    max_cluster = words(entry, 8, 1)[0]
    root_start = words(entry, 16, 1)[0]
    data_start = root_start + entry[11]
    require(entry[14] + fat_sectors == root_start and
            words(entry, 20, 1)[0] == data_start - 2 and
            data_start + max_cluster - 2 == symbols[f"M2K_{disk}_SECTORS"] and
            (max_cluster * 3 + 1) // 2 <= fat_sectors * SECTOR_SIZE,
            f"{drive}: FAT12, directory and data regions")


def verify_body(body, symbols):
    require(body[0] == 1 and body[128] == 0xC3, "LD body header or entry opcode")
    require(words(body, 18, 3) == (len(body) - 128, 0x8080, 0x8080), "LD header")
    require(symbols["OFFSET"] + len(body) == symbols["LAST_ADR"], "LD image size")
    require(symbols["LAST_ADR"] <= 0x10000, "LD image crosses FFFFh")
    require(symbols["WORKAD"] == symbols["CPM_BOOT"] == 0xF000, "BIOS address")
    require(symbols["_RDFATX"] == 0xF0E2, "FORMAT utility BIOS entry")
    require(symbols["_FAKEFREE"] == 0xF0AC, "FAKEFREE utility work address")
    require(symbols["STABLE"] == 0xF100 and symbols["_DEVICE"] == 0xF200,
            "BDOS table or DPB address")
    require(symbols["RESIDENT_END"] == 0xF300, "resident image overlaps work buffers")
    require(symbols["RESIDENT_END"] - symbols["BDOS"] ==
            symbols["LAST_ADR"] - symbols["INITE"], "resident relocation size")
    require(symbols["M2K_START"] < symbols["INITE"] < symbols["BDOS"],
            "resident copy would overlap the running initializer")
    require(symbols["BDOS"] == choose_bdos_page(symbols) + 6 and
            symbols["AT_WORK"] <= symbols["WORKAD"], "DOS is not on the highest usable page")
    require(symbols["M2K_TVRAM_SAFE_END"] <= 0xD000 or
            symbols["M2K_TVRAM_SAFE_BEGIN"] >= 0xE000, "TVRAM code or stack overlaps VRAM")
    require(symbols["M2K_VIDEO_STACK_END"] - symbols["M2K_VIDEO_STACK"] == 32,
            "TVRAM stack bounds")
    gram_code_size = symbols["M2K_GRAM_CODE_END"] - symbols["M2K_GRAM_CODE_IMAGE"]
    require(symbols["M2K_GRAM_READ_BYTE"] == 0x40 and
            symbols["M2K_GRAM_WRITE_BYTE"] == 0x47 and gram_code_size == 14,
            "GRAM page-zero entry or size")
    gram_code_offset = (symbols["INITE"] - symbols["OFFSET"] +
                        symbols["M2K_GRAM_CODE_IMAGE"] - symbols["BDOS"])
    require(body[gram_code_offset:gram_code_offset + gram_code_size] ==
            bytes.fromhex("D3 E8 46 79 D3 E8 C9 D3 E8 70 79 D3 E8 C9"),
            "GRAM byte routines must restore main RAM before touching the stack")
    verify_ram_disk(body, symbols, "E", "EMMFL", "EMM", ("EDRD", "EDWT"), b"EMM0")
    verify_ram_disk(body, symbols, "G", "GRAMFL", "GRAM", ("GDRD", "GDWT"), b"GRAM")


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


def assemble(assembler, stem, output, definitions=(), output_dir=ROOT, source_dir=ROOT):
    binary_path = output_dir / output
    symbol_path = output_dir / f"{stem}.SYM"
    command = [
        assembler, "-i", f"{stem}.ASM", "-bin", str(binary_path),
        "-sym", str(symbol_path), "-lst", str(output_dir / f"{stem}.LST"),
        "-f", "-gap", "0",
    ]
    if definitions:
        command.extend(("-dl", *definitions))
    subprocess.run(command, cwd=source_dir, check=True)
    return binary_path.read_bytes(), read_symbols(symbol_path)


def assemble_commands(assembler):
    commands = []
    with TemporaryDirectory() as command_dir:
        for name in COMMANDS:
            content, _ = assemble(assembler, name, f"{name}.COM",
                                  output_dir=Path(command_dir),
                                  source_dir=ROOT / "cmd" / name.lower())
            require(content[:1] == b"\xC3", f"{name}.COM has no Z80 entry jump")
            fat_name = name.encode("ascii").ljust(8) + b"COM"
            commands.append((fat_name, content, 0x20))
    return commands


def emm_base_from_env():
    configured = os.environ.get("M2K_EMM_BASE", "A0").strip()
    error = "M2K_EMM_BASE must be A0, A4, A8, or AC (hexadecimal)"
    try:
        base = int(configured, 16)
    except ValueError:
        raise SystemExit(error) from None
    if base not in (0xA0, 0xA4, 0xA8, 0xAC):
        raise SystemExit(error)
    return base


def main():
    assembler = os.environ.get("AILZ80ASM") or "AILZ80ASM.exe"
    emm_base = emm_base_from_env()
    emm_definition = f"M2K_EMM_BASE=${emm_base:02X}"
    with TemporaryDirectory() as probe_dir:
        _, symbols = assemble(assembler, "M2K", "M2K.BIN",
                              ("M2K_BDOS_PAGE=$A000", emm_definition),
                              Path(probe_dir))
    page = choose_bdos_page(symbols)
    body, symbols = assemble(assembler, "M2K", "M2K.BIN",
                             (f"M2K_BDOS_PAGE=${page:04X}", emm_definition))
    require(symbols["BDOS"] == page + 6, "assembler did not apply the selected DOS page")
    emm_ports = ("M2K_EMM_LOW", "M2K_EMM_MID", "M2K_EMM_HIGH", "M2K_EMM_DATA")
    require(tuple(symbols[port] for port in emm_ports) == tuple(range(emm_base, emm_base + 4)),
            "assembler did not apply the selected EMM ports")
    verify_body(body, symbols)
    print(f"BDOS {symbols['BDOS']:04X}h, TPA 0100h-{symbols['BDOS'] - 1:04X}h")
    print(f"EMM E: {emm_base:02X}h-{emm_base + 3:02X}h")
    mzt_path = DISK_DIR / "ldmz2000.mzt"
    mzt, symbols = assemble(assembler, "M2KMZT", str(mzt_path.relative_to(ROOT)))
    verify_mzt(body, mzt, symbols)
    ipl, symbols = assemble(assembler, "M2KIPL", "M2KIPL.BIN", (
        f"MZT_LBA={MZT_LBA}", f"MZT_SECTORS={sector_count(len(mzt))}",
        f"MZT_PAYLOAD_BYTES={len(mzt) - 128}",
    ))
    verify_ipl(ipl, symbols, mzt)
    commands = assemble_commands(assembler)
    target = DISK_DIR / "ldmz2000.d88"
    image = build_d88(ipl, mzt, commands)
    verify_d88(image, ipl, mzt, commands)
    target.write_bytes(image)
    print("Bundled COM commands: " + ", ".join(COMMANDS))
    print(f"Verified disk/{mzt_path.name} ({len(mzt)} bytes) and "
          f"disk/{target.name} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
