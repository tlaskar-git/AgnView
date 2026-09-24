"""Drives the running AgnView app on a CI runner, to check what unit tests
cannot: the menu bar icon's menu and the close button.

    python tools/macos/ui_check.py open-menu        click the menu bar icon, print the menu's window id
    python tools/macos/ui_check.py close-menu       press Escape
    python tools/macos/ui_check.py close-window     click the window's close button
    python tools/macos/ui_check.py window-visible   exit 0 when the main window is on screen

It posts mouse and key events with Quartz, which needs the runner's
accessibility permission. Used by .github/workflows/macos.yml only.
"""

import sys
import time

import Quartz

OWNER = "AgnView"


def windows(owner: str = OWNER):
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for info in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if info.get(Quartz.kCGWindowOwnerName) == owner:
            bounds = info.get(Quartz.kCGWindowBounds) or {}
            yield (
                int(info[Quartz.kCGWindowNumber]),
                int(info.get(Quartz.kCGWindowLayer, 0)),
                float(bounds.get("X", 0)),
                float(bounds.get("Y", 0)),
                float(bounds.get("Width", 0)),
                float(bounds.get("Height", 0)),
            )


def main_window():
    found = [w for w in windows() if w[1] == 0 and w[4] * w[5] >= 200 * 200]
    return max(found, key=lambda w: w[4] * w[5]) if found else None


def status_item():
    # The status item is a small window of AgnView's at the top of the screen,
    # above the normal window layer.
    for window in windows():
        _number, layer, x, y, width, height = window
        if layer > 0 and y < 40 and width < 80 and height < 40:
            return window
    return None


def click(x: float, y: float) -> None:
    point = Quartz.CGPointMake(x, y)
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.15)


def key(code: int) -> None:
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, code, down))
        time.sleep(0.1)


def wait_for(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    return None


def open_menu() -> int:
    item = wait_for(status_item)
    if item is None:
        print("No menu bar icon found", file=sys.stderr)
        return 1
    _number, _layer, x, y, width, height = item
    click(x + width / 2, y + height / 2)
    menu = wait_for(lambda: next((w for w in windows() if w[1] >= 100 and w[5] > 40), None), 5)
    if menu is None:
        print("The menu did not open", file=sys.stderr)
        return 1
    print(menu[0])
    return 0


def close_window() -> int:
    window = main_window()
    if window is None:
        print("No main window on screen", file=sys.stderr)
        return 1
    _number, _layer, x, y, _width, _height = window
    # The red close button sits about 20 points in and 14 down from the
    # window's top-left corner.
    click(x + 20, y + 14)
    if wait_for(lambda: main_window() is None, 10) is None:
        print("The window is still on screen", file=sys.stderr)
        return 1
    return 0


def main(argv) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "open-menu":
        return open_menu()
    if command == "close-menu":
        key(53)  # Escape
        return 0
    if command == "close-window":
        return close_window()
    if command == "window-visible":
        return 0 if wait_for(main_window, 15) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
