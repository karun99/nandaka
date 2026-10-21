"""Historical analysis: frequency, Topic Priority Score, negative marking
(FR-HA-*, FR-NM-*)."""

from .frequency import (  # noqa: F401
    compute_stats, diagram_frequency, is_near_duplicate, paper_set,
    question_years,
)
from .negative_marking import (  # noqa: F401
    AttemptAdvice, MarkingScheme, advise, break_even_p, expected_attempt,
    parse_marking_text,
)
from .tps import RULES_DOC, compute_tps  # noqa: F401
