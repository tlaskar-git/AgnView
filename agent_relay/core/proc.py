"""Start child processes without a console window on Windows.

The desktop app has no console of its own, so every command it starts, such
as claude, codex or agy (which are .cmd scripts on Windows), used to open a
new black console window for as long as it ran. Every place AgnView starts a
process passes these keyword arguments.
"""

import sys
from typing import Any, Dict

CREATE_NO_WINDOW = 0x08000000


def no_window() -> Dict[str, Any]:
    """Keyword arguments for subprocess and asyncio process calls."""
    if sys.platform == "win32":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}
