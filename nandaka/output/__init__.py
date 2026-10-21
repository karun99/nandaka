"""Output production: writers, batch report, metrics (F-OUTPUT)."""

from .writers import (  # noqa: F401
    WRITEABLE, validate_output, write_csv, write_md, write_output,
    write_pdf, write_txt, write_xlsx,
)
from .report import assemble_report, write_batch  # noqa: F401
from .metrics import (  # noqa: F401
    BASELINE_MINUTES, RunTimer, automation_coverage, build_report,
    quality_metrics, time_saved_pct,
)
from .pdfwriter import markdown_to_pdf  # noqa: F401
