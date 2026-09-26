"""Write the PyInstaller version resource for AgnView.exe.

  python tools/make-version-file.py OUTPUT_FILE

The version comes from agent_relay/__init__.py, the single source. The file is
read as text, so no dependency has to be installed to run this.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_version() -> str:
    text = (ROOT / "agent_relay" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("agent_relay/__init__.py has no __version__.")
    return match.group(1)


def version_tuple(version: str) -> tuple:
    numbers = [int(piece) for piece in re.findall(r"\d+", version.split("-")[0])[:4]]
    return tuple((numbers + [0, 0, 0, 0])[:4])


TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tup},
    prodvers={tup},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'AgnView'),
         StringStruct('FileDescription', 'AgnView'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'AgnView'),
         StringStruct('OriginalFilename', 'AgnView.exe'),
         StringStruct('ProductName', 'AgnView'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    version = read_version()
    Path(sys.argv[1]).write_text(TEMPLATE.format(tup=version_tuple(version), version=version), encoding="utf-8")
    print(f"Wrote {sys.argv[1]} for version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
