"""Add one 8.3 file to a FAT12 file stored in a D88 image.

The D88 image is treated as a sector container; its 512-byte logical sectors
are unpacked, the FAT12 metadata is updated, and the image is packed again.
The special 256-byte IPL sector in the MZ-2000 image is preserved unchanged.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


D88_HEADER_SIZE = 0x2B0
TRACK_COUNT = 80
SECTORS_PER_TRACK = 9
SECTOR_HEADER_SIZE = 16


def fail(message: str) -> None:
    raise ValueError(message)


def fat_get(fat: bytearray, cluster: int) -> int:
    offset = cluster * 3 // 2
    value = fat[offset] | (fat[offset + 1] << 8)
    return (value >> 4) & 0xFFF if cluster & 1 else value & 0xFFF


def fat_set(fat: bytearray, cluster: int, value: int) -> None:
    offset = cluster * 3 // 2
    old = fat[offset] | (fat[offset + 1] << 8)
    if cluster & 1:
        new = (old & 0x000F) | ((value & 0xFFF) << 4)
    else:
        new = (old & 0xF000) | (value & 0xFFF)
    fat[offset:offset + 2] = struct.pack("<H", new)


def unpack_d88(image: bytearray) -> tuple[list[bytearray], list[tuple[int, int]]]:
    sectors: list[bytearray] = []
    locations: list[tuple[int, int]] = []
    offset = D88_HEADER_SIZE
    for track in range(TRACK_COUNT):
        track_offset = struct.unpack_from("<I", image, 0x20 + track * 4)[0]
        if track_offset != offset:
            fail(f"unexpected D88 track offset at track {track}")
        for _ in range(SECTORS_PER_TRACK):
            if offset + SECTOR_HEADER_SIZE > len(image):
                fail("truncated D88 sector header")
            c, h, r, n, track_sectors, size = struct.unpack_from("<BBBBH8xH", image, offset)
            if (c, h) != (track // 2, track % 2) or track_sectors != SECTORS_PER_TRACK:
                fail(f"invalid D88 CHRN/track header at LBA {len(sectors)}")
            if size not in (256, 512):
                fail(f"unsupported D88 sector size {size} at LBA {len(sectors)}")
            data_start = offset + SECTOR_HEADER_SIZE
            data_end = data_start + size
            if data_end > len(image):
                fail("truncated D88 sector data")
            sectors.append(bytearray(image[data_start:data_end]))
            locations.append((offset, size))
            offset = data_end
    if offset != len(image):
        fail("unexpected trailing D88 data")
    return sectors, locations


def pack_d88(original: bytearray, sectors: list[bytearray], locations: list[tuple[int, int]]) -> bytearray:
    result = bytearray(original)
    for sector, (offset, size) in zip(sectors, locations):
        if len(sector) != size:
            fail("attempted to change a physical sector size")
        result[offset + SECTOR_HEADER_SIZE:offset + SECTOR_HEADER_SIZE + size] = sector
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="input D88 image")
    parser.add_argument("file", type=Path, help="host file to add")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="output D88 image")
    parser.add_argument("--name", default=None,
                        help="8.3 FAT name (default: host filename)")
    parser.add_argument("--replace", action="store_true",
                        help="replace an existing file with the same name")
    args = parser.parse_args()

    image = bytearray(args.image.read_bytes())
    payload = args.file.read_bytes()
    sectors, locations = unpack_d88(image)

    if len(sectors[0]) != 512:
        fail("FAT12 boot sector must be a 512-byte logical sector")
    bpb = sectors[0]
    bytes_per_sector, sectors_per_cluster, reserved, fat_count = struct.unpack_from(
        "<HBHB", bpb, 11
    )
    root_entries, total_sectors = struct.unpack_from("<HH", bpb, 17)
    sectors_per_fat = struct.unpack_from("<H", bpb, 22)[0]
    if (bytes_per_sector, reserved, fat_count) != (512, 1, 2):
        fail("unsupported FAT12 BPB (expected 512-byte sectors, 1 reserved, 2 FATs)")
    if total_sectors != len(sectors):
        fail("BPB total sector count does not match D88")
    if sectors_per_cluster == 0 or sectors_per_fat == 0:
        fail("invalid FAT12 BPB")

    fat_start = reserved
    root_start = fat_start + fat_count * sectors_per_fat
    root_sectors = (root_entries * 32 + 511) // 512
    data_start = root_start + root_sectors
    if data_start >= len(sectors):
        fail("invalid FAT12 layout")
    fat = bytearray().join(sectors[fat_start:fat_start + sectors_per_fat])
    name = (args.name or args.file.name).upper()
    if "." in name:
        stem, extension = name.rsplit(".", 1)
    else:
        stem, extension = name, ""
    if not (1 <= len(stem) <= 8 and len(extension) <= 3 and
            stem.isascii() and extension.isascii() and
            all(ch.isalnum() or ch in "_$~!#%&'()-@^`{}" for ch in stem + extension)):
        fail("file name must be a valid 8.3 ASCII FAT name")
    fat_name = f"{stem:<8}{extension:<3}".encode("ascii")

    root = bytearray().join(sectors[root_start:root_start + root_sectors])
    existing = [root[i:i + 32] for i in range(0, len(root), 32)]
    existing_index = next((i for i, entry in enumerate(existing)
                           if entry[:11] == fat_name and entry[0] not in (0xE5, 0x00)), None)
    if existing_index is not None and not args.replace:
        fail(f"file already exists: {name}")
    entry_index = existing_index if existing_index is not None else next(
        (i for i, entry in enumerate(existing) if entry[0] in (0x00, 0xE5)), None)
    if entry_index is None or entry_index >= root_entries:
        fail("root directory is full")

    cluster_size = bytes_per_sector * sectors_per_cluster
    cluster_count = max(1, (len(payload) + cluster_size - 1) // cluster_size)
    image_max_cluster = ((len(sectors) - data_start) // sectors_per_cluster) + 1
    fat_max_cluster = (2 * (len(fat) - 2)) // 3
    max_cluster = min(image_max_cluster, fat_max_cluster)
    if existing_index is not None:
        old_cluster = struct.unpack_from("<H", root, existing_index * 32 + 26)[0]
        seen = set()
        while 2 <= old_cluster <= max_cluster and old_cluster not in seen:
            seen.add(old_cluster)
            next_cluster = fat_get(fat, old_cluster)
            fat_set(fat, old_cluster, 0)
            if next_cluster >= 0xFF8:
                break
            old_cluster = next_cluster
    free = [cluster for cluster in range(2, max_cluster + 1)
            if fat_get(fat, cluster) == 0]
    if len(free) < cluster_count:
        fail("not enough free FAT12 space")
    chain = free[:cluster_count]

    for index, cluster in enumerate(chain):
        fat_set(fat, cluster, 0xFFF if index == len(chain) - 1 else chain[index + 1])
        start = data_start + (cluster - 2) * sectors_per_cluster
        chunk = payload[index * cluster_size:(index + 1) * cluster_size]
        chunk = chunk.ljust(cluster_size, b"\0")
        for sector_index in range(sectors_per_cluster):
            if len(sectors[start + sector_index]) != 512:
                fail(f"FAT cluster {cluster} maps to a non-512-byte D88 sector")
            sectors[start + sector_index][:] = chunk[sector_index * 512:(sector_index + 1) * 512]

    for copy in range(fat_count):
        start = fat_start + copy * sectors_per_fat
        for index in range(sectors_per_fat):
            sectors[start + index][:] = fat[index * 512:(index + 1) * 512]

    entry = bytearray(32)
    entry[:11] = fat_name
    entry[11] = 0x20
    struct.pack_into("<HI", entry, 26, chain[0], len(payload))
    root[entry_index * 32:(entry_index + 1) * 32] = entry
    for index in range(root_sectors):
        sectors[root_start + index][:] = root[index * 512:(index + 1) * 512]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(pack_d88(image, sectors, locations))
    print(f"added {name} ({len(payload)} bytes), clusters {chain[0]}..{chain[-1]}")
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
