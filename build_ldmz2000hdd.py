"""Create an MZ-2000 D88 image containing the LD-hdd driver."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "disk" / "ldmz2000.d88"
DEFAULT_OUTPUT = ROOT / "disk" / "ldmz2000hdd.d88"
DEFAULT_DRIVER = ROOT.parent / "LD-hdd" / "hddm2k.com"
FAT_TOOL = ROOT / "add_fat12_file.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--driver", type=Path, default=DEFAULT_DRIVER)
    args = parser.parse_args()

    for path, label in ((args.source, "source D88"),
                        (args.driver, "HDD driver"),
                        (FAT_TOOL, "FAT12 tool")):
        if not path.is_file():
            raise SystemExit(f"{label} not found: {path}")
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("output must differ from source")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.source, args.output)
    subprocess.run(
        [sys.executable, str(FAT_TOOL), str(args.output), str(args.driver),
         "--output", str(args.output), "--name", "HDDM2K.COM", "--replace"],
        check=True,
        cwd=ROOT,
    )
    print(f"copied {args.source} -> {args.output}")
    print(f"added {args.driver.name} as HDDM2K.COM")


if __name__ == "__main__":
    main()
