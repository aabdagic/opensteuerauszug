"""End-to-end test for the Coinbase importer with synthetic statement files.

Runs the full CLI pipeline against the mini Kursliste, which contains the
Bitcoin, USD Coin and Tether tokens with their official 2025 year-end values.
"""

from decimal import Decimal
from pathlib import Path

import lxml.etree as ET
from typer.testing import CliRunner

from opensteuerauszug.steuerauszug import app
from tests.importers.coinbase.statement_data import (
    ACCOUNT_ID,
    HOLDINGS_2025,
    PRICES_2025,
    YEAR_2025,
    statement_html,
    transactions_csv,
)

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_TOML = f"""
[general]
full_name = "Erika Mustermann"
canton = "ZH"
experimental_importers = true

[brokers.coinbase.accounts.main]
account_number = "{ACCOUNT_ID}"
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


def _run(tmp_path: Path, rows=YEAR_2025, identifiers="", **statement_kwargs):
    statements = tmp_path / "coinbase_2025"
    statements.mkdir()
    (statements / "statement_2025.htm").write_text(
        statement_html(rows=rows, **statement_kwargs), encoding="utf-8"
    )
    (statements / "transactions_2025.csv").write_text(transactions_csv(rows), encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TOML, encoding="utf-8")
    identifiers_csv = tmp_path / "security_identifiers.csv"
    identifiers_csv.write_text("symbol,isin,valor\n" + identifiers, encoding="utf-8")
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
            "--identifiers-csv-path",
            str(identifiers_csv),
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


def test_token_missing_from_the_kursliste_keeps_its_value_and_income(tmp_path: Path, caplog):
    rows = YEAR_2025 + [
        ("2025-04-01 10:00:00 UTC", "Buy", "ZZZ", "100", "USD", "$2.00", "$0.00",
         "$200.00", "$200.00", "Bought 100 ZZZ for 200 USD using Visa ****0000"),
        ("2025-05-01 10:00:00 UTC", "Reward Income", "ZZZ", "5", "USD", "$2.00", "$0.00",
         "$10.00", "$10.00", "Received 5 ZZZ from Coinbase Rewards"),
    ]  # fmt: skip
    result, xml_doc = _run(
        tmp_path,
        rows=rows,
        holdings=dict(HOLDINGS_2025, ZZZ="105"),
        prices=dict(PRICES_2025, ZZZ="3.00"),
    )
    zzz = _securities(xml_doc)["ZZZ"]
    tax_value = zzz.find(_ech("taxValue"))
    # Coinbase's year-end price (105 x 3.00 USD), converted to CHF, flagged as not from the Kursliste
    assert Decimal(tax_value.get("value")) == Decimal("315.00") * Decimal(
        tax_value.get("exchangeRate")
    )
    assert tax_value.get("kursliste") not in ("1", "true")
    [payment] = zzz.findall(_ech("payment"))
    assert Decimal(payment.get("amount")) == Decimal("10.00")
    assert Decimal(payment.get("grossRevenueB")) > 0
    assert "ZZZ was not found in the Kursliste" in caplog.text or "  - ZZZ" in caplog.text
    assert "mismatches=0" in result.stdout


def test_identifiers_file_entry_with_an_isin_does_not_hijack_a_token(tmp_path: Path):
    # E.g. a Bitcoin ETF traded under the ticker "BTC" (a fund from the mini Kursliste here)
    _, xml_doc = _run(tmp_path, identifiers="BTC,US9220427424,4354003\n")
    btc = _securities(xml_doc)["BTC"]
    assert btc.get("isin") is None
    assert btc.get("valorNumber") == "39714275"


def test_kursliste_token_far_from_the_broker_value_is_a_critical_warning(tmp_path: Path, caplog):
    _run(tmp_path, prices=dict(PRICES_2025, BTC="1.00"))
    assert "The Kursliste value of Bitcoin" in caplog.text
    assert "differs strongly" in caplog.text


def test_plausible_broker_values_give_no_token_warning(tmp_path: Path, caplog):
    _run(tmp_path)
    assert "differs strongly" not in caplog.text
