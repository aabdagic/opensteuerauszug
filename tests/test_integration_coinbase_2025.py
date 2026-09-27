"""End-to-end test for the Coinbase importer with synthetic statement files.

Runs the full CLI pipeline against the mini Kursliste, which contains the
Bitcoin, USD Coin and Tether tokens with their official 2025 year-end values.
"""

from decimal import Decimal
from pathlib import Path

import lxml.etree as ET
from typer.testing import CliRunner

from opensteuerauszug.steuerauszug import app
from tests.importers.coinbase.statement_data import statement_html, transactions_csv

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_TOML = """
[general]
full_name = "Erika Mustermann"
canton = "ZH"
experimental_importers = true

[brokers.coinbase.accounts.main]
account_number = "COINBASE"
"""


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


def _run(tmp_path: Path):
    statements = tmp_path / "coinbase_2025"
    statements.mkdir()
    (statements / "statement_2025.htm").write_text(statement_html(), encoding="utf-8")
    (statements / "transactions_2025.csv").write_text(transactions_csv(), encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TOML, encoding="utf-8")
    output_xml = tmp_path / "coinbase.xml"
    result = runner.invoke(
        app,
        [
            "process",
            str(statements),
            "--importer",
            "coinbase",
            "--tax-year",
            "2025",
            "--config",
            str(config),
            "--kursliste-dir",
            str(PROJECT_ROOT / "tests" / "samples" / "kursliste"),
            "--output",
            str(tmp_path / "coinbase.pdf"),
            "--xml-output",
            str(output_xml),
        ],
    )
    assert result.exit_code == 0, f"CLI execution failed with stdout:\n{result.stdout}"
    return result, ET.parse(str(output_xml))


def test_coinbase_statement_produces_schema_valid_output(tmp_path: Path):
    result, xml_doc = _run(tmp_path)
    assert "Coinbase import complete." in result.stdout

    specs_dir = PROJECT_ROOT / "specs"
    parser = ET.XMLParser()
    parser.resolvers.add(_LocalXsdResolver(specs_dir))
    schema = ET.XMLSchema(ET.parse(str(specs_dir / "eCH-0196-2-2.xsd"), parser=parser))
    assert schema.validate(xml_doc), f"XSD validation failed:\n{schema.error_log}"


def _securities(xml_doc):
    # The XML has no symbol attribute; the importer names each security by its ticker.
    return {s.get("securityName"): s for s in xml_doc.getroot().iter(_ech("security"))}


def test_tokens_are_found_by_ticker_and_valued_at_the_kursliste_year_end_value(
    tmp_path: Path, caplog
):
    _, xml_doc = _run(tmp_path)
    by_symbol = _securities(xml_doc)
    btc_value = by_symbol["BTC"].find(_ech("taxValue"))
    assert by_symbol["BTC"].get("valorNumber") == "39714275"
    assert by_symbol["USDC"].get("valorNumber") == "121082241"
    # Tokens are resolved by ticker in the Kursliste, not via security_identifiers.csv.
    assert "could not be mapped" not in caplog.text
    assert Decimal(btc_value.get("unitPrice")) == Decimal("69571.988489")
    assert Decimal(btc_value.get("value")) == Decimal("0.032") * Decimal("69571.988489")
    assert btc_value.get("kursliste") in ("1", "true")


def test_reward_income_is_kept_as_taxable_income_in_chf(tmp_path: Path):
    result, xml_doc = _run(tmp_path)
    usdc = _securities(xml_doc)["USDC"]
    payments = usdc.findall(_ech("payment"))
    assert [p.get("paymentDate") for p in payments] == ["2025-05-01", "2025-06-01"]
    for p in payments:
        assert Decimal(p.get("grossRevenueB")) == Decimal(p.get("amount")) * Decimal(
            p.get("exchangeRate")
        )
        assert Decimal(p.get("grossRevenueA")) == 0
    assert "matches=2, capped=0, expected-missing=0, mismatches=0" in result.stdout
