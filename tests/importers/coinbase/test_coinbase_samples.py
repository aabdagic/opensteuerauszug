"""Run the Coinbase importer on real statements kept outside the repository.

Put one tax year per directory under ``private/samples/import/coinbase/<year>/``
(or ``$EXTRA_SAMPLE_DIR/import/coinbase/<year>/``): the yearly HTML statement
and optionally the CSV export of the same year.
"""

from datetime import date
from pathlib import Path

import pytest

from opensteuerauszug.importers.coinbase.coinbase_importer import (
    FIAT_CURRENCIES,
    CoinbaseImporter,
)
from opensteuerauszug.importers.coinbase.statement_parser import parse_statement_html
from tests.utils.samples import get_sample_dirs

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("sample_dir", get_sample_dirs("import/coinbase", [".htm", ".html"]))
def test_sample_statement_imports_with_its_year_end_holdings(sample_dir: str):
    statements = [
        parse_statement_html(p.read_bytes(), str(p))
        for p in sorted(Path(sample_dir).iterdir())
        if p.suffix.lower() in (".htm", ".html")
    ]
    yearly = [
        s
        for s in statements
        if s.period_start.month == 1
        and s.period_start.day == 1
        and s.holdings_as_of is not None
        and (s.holdings_as_of.month, s.holdings_as_of.day) == (12, 31)
    ]
    assert yearly, f"No yearly statement in {sample_dir}"
    year = yearly[0].period_start.year

    tax_statement = CoinbaseImporter(date(year, 1, 1), date(year, 12, 31), []).import_dir(
        sample_dir
    )

    closing = {
        s.securityName: next(
            st.quantity for st in s.stock if not st.mutation and st.referenceDate.year == year + 1
        )
        for s in tax_statement.listOfSecurities.depot[0].security
    }
    expected = {a: q for a, q in yearly[0].holdings.items() if a not in FIAT_CURRENCIES}
    assert {a: q for a, q in closing.items() if q} == expected
