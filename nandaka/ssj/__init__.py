"""SSJ guardrail engine (§3.2). Cannot be disabled (CO-06)."""

from .guardrail import (  # noqa: F401
    ALL_TAGS, EVIDENCE_LABELS, EXCLUDED, NA, PASS, REVIEW,
    Finding, SSJResult, check_image, check_text, compliance_report,
    overall_tag, REPORT_CHECKS,
)
