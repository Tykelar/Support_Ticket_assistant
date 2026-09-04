"""Demo aids: a curated set of tickets, and a page that shows what the pipeline did.

Not part of the service. Nothing here is imported by the pipeline, the guardrails or the
storage layer -- this package is data, static files, and an HTTP client. `api/app.py`
mounts `static/` at `/ui`; that mount is the only wire between this package and the
running service (DEMO.md).

The scenarios live under `static/` because the browser fetches the same file the CLI
seeder and the tests read. One copy, so a scenario cannot be fixed in one place and stay
broken in the other.
"""

import json
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
SCENARIOS_FILE = STATIC_DIR / "scenarios.json"


def load_scenarios() -> list[dict[str, Any]]:
    """The curated tickets, in file order. Read on every call, like the tool fixtures
    (TOOLS.md): a reviewer edits a scenario and re-runs without a restart."""
    return json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
