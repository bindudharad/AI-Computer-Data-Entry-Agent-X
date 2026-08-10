"""Universal run performance report.

A structured snapshot of one universal-attach run: target, environment,
adapter, field counts, timings, method usage and - critically - the launch
guard numbers (``attach_count``, ``launch_count``) proving we never launched
an already-existing target.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UniversalPerformanceReport:
    target: str = ""
    environment: str = ""
    adapter: str = ""
    attach_mode: str = ""          # EXISTING_WINDOW / EXISTING_TAB / BROWSER_UIA / ...
    field_count: int = 0
    verified_count: int = 0
    total_ms: float = 0.0
    attach_count: int = 0
    launch_count: int = 0          # MUST stay 0 when a target already existed
    method_usage: dict[str, int] = field(default_factory=dict)  # method -> uses
    notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        """A run is OK when an existing target was never relaunched.

        For EXISTING_* attachment modes a launch is always a defect. For
        NEW_LAUNCH / USER_ATTACH the launch count is expected to reflect the
        chosen attachment path, so the run is OK.
        """
        if self.attach_mode.startswith("EXISTING"):
            return self.launch_count == 0
        return True

    @property
    def avg_field_ms(self) -> float:
        return round(self.total_ms / self.field_count, 1) if self.field_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["avg_field_ms"] = self.avg_field_ms
        data["ok"] = self.ok
        return data

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_file(cls, path: str | Path) -> "UniversalPerformanceReport":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


__all__ = ["UniversalPerformanceReport"]
