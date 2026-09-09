"""AuditLogger: append-only JSONL decision log, per Requirement 5.

Exactly one record is written per symbol per orchestrator cycle, including
HOLD and rejected outcomes, so the log explains inaction as well as action.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str))
            fh.write("\n")
            fh.flush()
