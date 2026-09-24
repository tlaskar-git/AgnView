# Architecture Decision Record: macOS Desktop App (ADR-MACOS-DESKTOP)

## Context & Problem Statement

The Windows desktop app (`agent_relay/desktop/app.py`) is a thin shell
around the hub: a pywebview window over the hub on loopback, a pystray tray
icon, a sign-in task, one instance per user, and a port that moves when
18845 is busy. The macOS app must do the same things in the Mac way, with
no change to the Windows app.

On macOS, AppKit must run on the main thread, and there is one run loop.
pywebview's Cocoa backend runs `NSApplication.run()` on the main thread
inside `webview.start()`. pystray's macOS backend wants to own the main
thread and its run loop as well. Running both side by side either blocks one
of them or creates the status item off the main thread, which AppKit does not
allow.

## Decision

### Menu bar icon: a native NSStatusItem in pywebview's run loop

- `agent_relay/desktop/macos_app.py` makes the status item with PyObjC's
  AppKit, which pywebview already depends on. pystray is not used on macOS.
- `webview.start(func=...)` calls a function on a worker thread as the run
  loop starts. That function posts the menu bar set-up to the main thread
  with `PyObjCTools.AppHelper.callAfter`, so the status item is created on
  the main thread, inside the run loop pywebview runs. One run loop, one main
  thread, no second GUI toolkit.
- The menu has Open AgnView, Start at login, Allow phones on my network and
  Quit AgnView, the same items as the Windows tray. The two switches show a
  tick, refreshed each time the menu opens.
- The menu bar image is the transparent logo (`agnview-icon.png`) as a
  template image, so macOS draws it in the menu bar's own colour.

### Dock icon: shown while the window is open

`Info.plist` sets `LSUIElement` to true, so AgnView starts as a menu bar app
with no Dock icon and no Dock bounce while Python starts. The app switches
to the regular activation policy when the window opens, and back to the
accessory policy when the window is hidden. The result:

- A window that is open behaves like any Mac app window: a Dock icon, Cmd+Tab,
  and the app menu with Edit shortcuts.
- A hidden AgnView lives in the menu bar only, as it lives in the tray on
  Windows. There is never a Dock icon that opens nothing.

### Quitting

AgnView replaces pywebview's application delegate with its own once the run
loop starts. `applicationShouldTerminate:` stops the hub and lets go of the
single-instance lock, then lets macOS end the process. Quit AgnView in the
menu, Cmd+Q, Quit in the Dock and logging out all take this path. The close
button still hides the window, as on Windows.

### Start at login: a LaunchAgent

- Start at login writes `~/Library/LaunchAgents/com.agnview.desktop.plist`
  with `RunAtLoad`, `KeepAlive` off and `LimitLoadToSessionType` Aqua. It
  starts the app's own executable with `--minimized`.
- The plist is written or removed and nothing more. launchd reads it at the
  next login. Calling `launchctl load` would start a second copy at once, and
  `launchctl bootout` would stop an app that launchd started.
- It is off by default and saved in `~/.agnview/desktop.json`, the same file
  and the same `change_autostart` path the Windows app and the dashboard's
  switch use. The menu, the dashboard and the file can never disagree.
- The label differs from `com.agnview.hub`, the LaunchAgent `agnview serve`
  writes. Turning Start at login on or off removes that one, as the Windows
  app removes the old Run key value, so a browser-only hub does not start
  at login beside the app.

### One instance per user

An exclusive `flock` on `~/.agnview/desktop.lock`. The kernel drops it when
the process ends, however it ends. A second launch that cannot take the lock
sends `show` over the Unix socket `~/.agnview/desktop.sock` and exits, and
the first instance brings its window forward. Opening the app from Finder
while it runs goes through `applicationShouldHandleReopen:` instead and does
the same.

### Everything else is shared

Settings, the port (18845, then the next free one in 20), the hub on
loopback or on every interface for Allow phones on my network, the QR code
that follows that mode, and the hub restart when the mode changes are the
code in `app.py`. Child processes already start without a console on macOS:
the agent chats run headless through `asyncio.create_subprocess_exec`, and
nothing opens Terminal.

### Packaging

PyInstaller, `--windowed --onedir`, which produces `AgnView.app`, built by
`tools/build-macos.sh` and packed into `AgnView-macos.dmg` with `hdiutil`.
PyInstaller is already the Windows build tool and handles pywebview, PyObjC
and the iroh extension module. The app is signed with the hardened runtime
(`tools/macos/entitlements.plist`) when a Developer ID is available, and
notarised by `.github/workflows/macos.yml`. See `docs/MACOS-SIGNING.md`.

The CI build targets the runner's architecture, Apple silicon.

## Consequences

- No new GUI dependency on macOS: pywebview brings PyObjC.
- pywebview's delegate is replaced. It only handled the quit confirmation
  AgnView does not use, and the secure restorable state flag, which the new
  delegate keeps.
- The first-hide notice uses `NSUserNotification`, which is deprecated. It
  is best effort, and the menu bar icon is visible either way.
- Linux still has no desktop app. `agnview-desktop` on Linux says so and
  exits, as before.
