"""Prints the window number of AgnView's main window, for screencapture -l.

    python tools/macos/find_window.py [owner name] [timeout seconds]

Waits until a normal window owned by the process appears on screen, so a
screenshot catches the app window only and nothing else on the desktop.
Prints "<window id> <screen width> <screen height>" and exits 0, or exits 1
when no window appears in time. Used by .github/workflows/macos.yml.
"""

import sys
import time

import Quartz


def find(owner: str):
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    best = None
    for info in windows:
        if info.get(Quartz.kCGWindowOwnerName) != owner:
            continue
        if info.get(Quartz.kCGWindowLayer, 1) != 0:
            continue
        bounds = info.get(Quartz.kCGWindowBounds) or {}
        area = float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))
        if area < 200 * 200:
            continue
        if best is None or area > best[1]:
            best = (int(info[Quartz.kCGWindowNumber]), area)
    return best[0] if best else None


def main(argv) -> int:
    owner = argv[1] if len(argv) > 1 else "AgnView"
    timeout = float(argv[2]) if len(argv) > 2 else 60.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window = find(owner)
        if window is not None:
            bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            print(window, int(bounds.size.width), int(bounds.size.height))
            return 0
        time.sleep(1)
    print(f"No window owned by {owner} appeared in {timeout:.0f} seconds", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
