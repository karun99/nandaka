"""Performance & quality metrics (FR-MET-01..04).

Baseline procedure-documentation time is fixed at 120 minutes (FR-MET-01).
Measured results are strictly separated from estimates/targets (FR-MET-04).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

BASELINE_MINUTES = 120.0

PHASES = [
    "source prep", "extraction", "research", "drafting", "MCQ generation",
    "validation", "human review", "rework", "image sourcing",
    "image generation", "image review",
]


@dataclass
class RunTimer:
    started: float = field(default_factory=time.time)
    phases: dict[str, float] = field(default_factory=dict)
    _open: dict[str, float] = field(default_factory=dict)

    def start(self, phase: str) -> None:
        self._open[phase] = time.time()

    def stop(self, phase: str) -> float:
        t0 = self._open.pop(phase, None)
        if t0 is None:
            return 0.0
        dur = time.time() - t0
        self.phases[phase] = self.phases.get(phase, 0.0) + dur
        return dur

    @property
    def total_minutes(self) -> float:
        return (time.time() - self.started) / 60.0

    def as_minutes(self) -> dict[str, float]:
        return {k: round(v / 60.0, 2) for k, v in self.phases.items()}


def time_saved_pct(actual_minutes: float,
                   baseline_minutes: float = BASELINE_MINUTES) -> float:
    """FR-MET-02."""
    if baseline_minutes <= 0:
        return 0.0
    return round((baseline_minutes - actual_minutes) / baseline_minutes * 100.0, 1)


def automation_coverage(automated_units: int, total_eligible: int) -> float:
    """FR-MET-02."""
    if total_eligible <= 0:
        return 0.0
    return round(100.0 * automated_units / total_eligible, 1)


def quality_metrics(*, items_total: int = 0, factual_errors: int = 0,
                    duplicates: int = 0, translation_defects: int = 0,
                    unsupported_claims: int = 0, human_interventions: int = 0,
                    ssj_review: int = 0, ssj_excluded: int = 0,
                    images_total: int = 0, images_correct: int = 0,
                    images_cleared: int = 0, images_with_coverage: int = 0,
                    topics_total: int = 0) -> dict[str, Any]:
    """FR-MET-03: rates as percentages, counts absolute."""
    def rate(n: int, d: int) -> float:
        return round(100.0 * n / d, 1) if d else 0.0
    return {
        "items_total": items_total,
        "factual_error_rate_pct": rate(factual_errors, items_total),
        "duplication_rate_pct": rate(duplicates, items_total),
        "translation_defect_rate_pct": rate(translation_defects, items_total),
        "unsupported_claims": unsupported_claims,
        "human_intervention_rate_pct": rate(human_interventions, items_total),
        "ssj_review_rate_pct": rate(ssj_review, items_total),
        "ssj_excluded_count": ssj_excluded,
        "image_accuracy_rate_pct": rate(images_correct, images_total),
        "image_licence_clearance_rate_pct": rate(images_cleared, images_total),
        "visual_coverage_rate_pct": rate(images_with_coverage, topics_total),
    }


def build_report(*, timer: RunTimer, status_counts: dict[str, int],
                 quality: dict[str, Any], coverage: dict[str, float],
                 estimates: dict[str, Any] | None = None) -> dict[str, Any]:
    """FR-MET-01/04: one metrics structure for the batch report."""
    actual = timer.total_minutes
    return {
        "measurement": {
            "baseline_minutes": BASELINE_MINUTES,
            "actual_minutes": round(actual, 2),
            "time_saved_pct": time_saved_pct(actual),
            "phases_minutes": timer.as_minutes(),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "automation": {
            "status_counts": status_counts,
            "automation_coverage_pct": automation_coverage(
                status_counts.get("VALIDATED", 0),
                sum(status_counts.values())),
        },
        "quality": quality,
        "visual_coverage": coverage,
        "note": "measurement = observed in this run; anything in `estimates` "
                "is a target, not a measurement (FR-MET-04)",
        "estimates": estimates or {},
    }
