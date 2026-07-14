"""Import report: what got backfilled from the bulk export vs. what still
needs a live API fetch. Printed at the end of every import run so nothing
silently falls through the cracks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CategoryResult:
    category: str
    files_found: int = 0
    records_seen: int = 0
    rows_written: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    results: dict[str, CategoryResult] = field(default_factory=dict)
    not_populated: list[str] = field(default_factory=list)  # schema areas the export can't fill at all

    def category(self, name: str) -> CategoryResult:
        if name not in self.results:
            self.results[name] = CategoryResult(category=name)
        return self.results[name]

    def flag_not_populated(self, schema_area: str, reason: str) -> None:
        self.not_populated.append(f"{schema_area}: {reason}")

    def render(self) -> str:
        lines = ["Garmin bulk export import report", "=" * 40]
        for name, r in sorted(self.results.items()):
            lines.append(
                f"[{name}] files={r.files_found} records_seen={r.records_seen} "
                f"rows_written={r.rows_written} skipped={r.skipped}"
            )
            for note in r.notes:
                lines.append(f"    note: {note}")
        if self.not_populated:
            lines.append("")
            lines.append("NOT populated by this export (needs a live API sync):")
            for item in self.not_populated:
                lines.append(f"  - {item}")
        return "\n".join(lines)
