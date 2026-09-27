"""End-to-end test for the Morgan Stanley at Work importer.

Runs the full CLI pipeline on the quarterly statements in
``tests/samples/import/morganstanley/anonymized_2025``: real 2025 statement
PDFs anonymised with ``scripts/anonymize_morganstanley_statements.py`` (fake
identity, re-randomised quantities, all amounts recomputed). They are checked
against the mini Kursliste, whose Alphabet Class C entry carries the real 2025
dividends.
"""

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import lxml.etree as ET
from typer.testing import CliRunner

from opensteuerauszug.importers.morganstanley.statement_parser import (
    ActivityKind,
    load_statement_pdf,
)
from opensteuerauszug.steuerauszug import app

runner = CliRunner()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = PROJECT_ROOT / "tests" / "samples" / "import" / "morganstanley" / "anonymized_2025"

CONFIG_TOML = """
[general]
full_name = "Erika Mustermann"
canton = "ZH"
experimental_importers = true

[brokers.morganstanley.accounts.gsu]
account_number = "MS12345678"
"""


def _shares_held_before(day: date) -> Decimal:
    """Share count at the start of *day*, walked from the sample statements."""
    statements = sorted(
        (load_statement_pdf(str(p)) for p in SAMPLE_DIR.glob("*.pdf")),
        key=lambda st: st.opening_date,
    )
    held = statements[0].opening_shares
    for st in statements:
        for line in st.lines:
            if line.trade_date >= day:
                return held
            if line.kind == ActivityKind.RELEASE and len(line.numbers) == 2:
                held += line.numbers[0]
            elif line.kind == ActivityKind.SALE:
                held += line.numbers[0]
    return held


class _LocalXsdResolver(ET.Resolver):
    def __init__(self, specs_dir: Path) -> None:
        super().__init__()
        self._specs_dir = specs_dir

    def resolve(self, url, pubid, context):
        if not url:
            return None
        candidate = self._specs_dir / url.rsplit("/", 1)[-1]
        if candidate.exists():
            return self.resolve_filename(str(candidate), context)
        return None


def _ech(tag: str) -> str:
    return f"{{http://www.ech.ch/xmlns/eCH-0196/2}}{tag}"


def _run(tmp_path: Path, *extra_args: str, input_dir: Path = SAMPLE_DIR):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML, encoding="utf-8")
    output_pdf = tmp_path / "ms_2025.pdf"
    output_xml = tmp_path / "ms_2025.xml"
    result = runner.invoke(
        app,
        [
            "process",
            str(input_dir),
            "--importer",
            "morganstanley",
            "--tax-year",
            "2025",
            "--config",
            str(config_path),
            "--kursliste-dir",
            str(PROJECT_ROOT / "tests" / "samples" / "kursliste"),
            "--output",
            str(output_pdf),
            "--xml-output",
            str(output_xml),
            *extra_args,
        ],
    )
    return result, output_pdf, output_xml


def test_morganstanley_statements_produce_schema_valid_statement(tmp_path: Path):
    result, output_pdf, output_xml = _run(tmp_path)

    assert result.exit_code == 0, f"CLI execution failed with stdout:\n{result.stdout}"
    assert "Morgan Stanley import complete." in result.stdout
    assert "Processing finished successfully." in result.stdout
    assert output_pdf.exists() and output_pdf.stat().st_size > 0

    specs_dir = PROJECT_ROOT / "specs"
    xsd_parser = ET.XMLParser()
    xsd_parser.resolvers.add(_LocalXsdResolver(specs_dir))
    schema = ET.XMLSchema(ET.parse(str(specs_dir / "eCH-0196-2-2.xsd"), parser=xsd_parser))
    xml_doc = ET.parse(str(output_xml))
    assert schema.validate(xml_doc), f"XSD validation failed:\n{schema.error_log}"

    root = xml_doc.getroot()
    assert root.get("canton") == "ZH"
    (security,) = root.findall(f".//{_ech('security')}")
    assert security.get("isin") == "US02079K1079"
    assert security.get("valorNumber") == "29798545"


def test_morganstanley_dividends_reconcile_with_kursliste(tmp_path: Path):
    """Broker dividends and the netted 15% withholding match the Kursliste."""
    result, _, output_xml = _run(tmp_path)

    assert result.exit_code == 0, f"CLI execution failed with stdout:\n{result.stdout}"
    assert "matches=2, capped=0, expected-missing=0, mismatches=0" in result.stdout

    security = ET.parse(str(output_xml)).getroot().find(f".//{_ech('security')}")
    payments = security.findall(_ech("payment"))
    # Kursliste payments use the holdings at the ex-dates (10 March / 9 June).
    assert [(p.get("paymentDate"), Decimal(p.get("quantity"))) for p in payments] == [
        ("2025-03-17", _shares_held_before(date(2025, 3, 10))),
        ("2025-06-16", _shares_held_before(date(2025, 6, 9))),
    ]
    assert all(p.get("lumpSumTaxCredit") in ("1", "true") for p in payments)


def test_unrelated_pdfs_in_the_input_directory_are_skipped(tmp_path: Path):
    input_dir = tmp_path / "statements"
    shutil.copytree(SAMPLE_DIR, input_dir)
    shutil.copy(PROJECT_ROOT / "docs" / "sample_output.pdf", input_dir / "steuerauszug.pdf")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result, _, _ = _run(output_dir, input_dir=input_dir)

    assert result.exit_code == 0, f"CLI execution failed with stdout:\n{result.stdout}"
    assert "Processing finished successfully." in result.stdout


def test_original_statements_can_be_appended_to_the_pdf(tmp_path: Path):
    from pypdf import PdfReader

    plain, plain_pdf, _ = _run(tmp_path)
    assert plain.exit_code == 0, plain.stdout
    plain_pages = len(PdfReader(str(plain_pdf)).pages)
    appendix = SAMPLE_DIR / "quarterly_statement_2025-12-31.pdf"

    appended_dir = tmp_path / "appended"
    appended_dir.mkdir()
    result, output_pdf, _ = _run(appended_dir, "--post-amble", str(appendix))
    assert result.exit_code == 0, result.stdout
    assert len(PdfReader(str(output_pdf)).pages) == plain_pages + len(
        PdfReader(str(appendix)).pages
    )
