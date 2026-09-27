"""Render the synthetic Morgan Stanley statements into sample PDFs.

The PDFs in ``tests/samples/import/morganstanley/synthetic_2025`` are produced
by this script from :mod:`statement_text`, so the integration test exercises
the real PDF text-extraction path without committing any personal data.
Re-run after changing the synthetic statements::

    uv run python -m tests.importers.morganstanley.make_sample_pdfs
"""

from pathlib import Path
from typing import Sequence

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from .statement_text import Q1_2025, Q2_2025, Q3_2025, Q4_2025

SAMPLE_DIR = (
    Path(__file__).resolve().parents[2] / "samples" / "import" / "morganstanley" / "synthetic_2025"
)

# A non-statement PDF in the same directory; the importer must skip it.
FORM_1042S = [
    "Form 1042-S Foreign Person's U.S. Source Income Subject to Withholding 2025\n"
    "1 Income code 06 2 Gross income 15.00\n"
    "3b Tax rate 15.00 7a Federal tax withheld 2.00\n"
]


def write_pdf(path: Path, pages: Sequence[str]) -> None:
    # invariant=1 keeps the output byte-identical between runs.  The output is
    # plain-text PDF; the .gitattributes next to the samples marks it binary so
    # git never rewrites its line endings (which would break the xref offsets).
    pdf = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    _, height = LETTER
    for page in pages:
        text = pdf.beginText(40, height - 50)
        text.setFont("Helvetica", 9)
        for line in page.splitlines():
            text.textLine(line)
        pdf.drawText(text)
        pdf.showPage()
    pdf.save()


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for name, pages in (
        ("quarterly_statement_2025-03-31.pdf", Q1_2025),
        ("quarterly_statement_2025-06-30.pdf", Q2_2025),
        ("quarterly_statement_2025-09-30.pdf", Q3_2025),
        ("quarterly_statement_2025-12-31.pdf", Q4_2025),
        ("form_1042s_2025.pdf", FORM_1042S),
    ):
        write_pdf(SAMPLE_DIR / name, pages)
        print(f"wrote {SAMPLE_DIR / name}")


if __name__ == "__main__":
    main()
