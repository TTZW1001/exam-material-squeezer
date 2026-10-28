from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class CompressionError(RuntimeError):
    """A user-facing compression failure."""


class CancelledError(RuntimeError):
    """Raised when the user stops a running batch."""


@dataclass
class CompressionResult:
    source: Path
    output: Path
    original_size: int
    final_size: int
    method: str
    outputs: tuple[Path, ...] = ()
    pages: int = 1
    total_output_size: int = 0

    @property
    def output_count(self) -> int:
        return len(self.outputs) if self.outputs else 1

    @property
    def saved_percent(self) -> int:
        if not self.original_size:
            return 0
        produced = self.total_output_size or self.final_size
        return max(0, round((1 - produced / self.original_size) * 100))
