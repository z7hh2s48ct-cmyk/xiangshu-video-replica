from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationTaskLease:
    id: str
    status: str
    attempt: int
    locked_by: str
    locked_until: str
