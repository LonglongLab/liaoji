"""Independent product versions, loaded from one packaged JSON authority.

The desktop compatibility alias never imports either runtime. Wheels and
embedded installers must retain release_versions.json beside this module.
"""

from __future__ import annotations

import json
from pathlib import Path

_VERSIONS: dict[str, str] = json.loads(Path(__file__).with_name("release_versions.json").read_text(encoding="utf-8"))
DESKTOP_VERSION = _VERSIONS["desktop"]
SERVER_VERSION = _VERSIONS["server"]
# Historical callers are the desktop updater/local HTTP layer.
APP_VERSION = DESKTOP_VERSION
