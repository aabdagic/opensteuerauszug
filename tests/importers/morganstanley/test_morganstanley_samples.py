"""Run the Morgan Stanley importer on every sample directory of quarterly statements.

Covers the committed synthetic PDFs under ``tests/samples/import/morganstanley``
and, when present, real statements in ``private/samples/import/morganstanley/<dir>``
or ``$EXTRA_SAMPLE_DIR/import/morganstanley/<dir>`` (one tax year per directory).
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from opensteuerauszug.importers.morganstanley.morganstanley_importer import (
    MorganStanleyImporter,
)
from opensteuerauszug.importers.morganstanley.statement_parser import load_statement_pdf
from tests.utils.samples import get_sample_dirs

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("sample_dir", get_sample_dirs("import/morganstanley", [".pdf"]))
def test_sample_statements_import_with_consistent_year_end_balances(sample_dir: str):
    statements = [
        st
        for st in (load_statement_pdf(str(p)) for p in sorted(Path(sample_dir).glob("*.pdf")))
        if st is not None
    ]
    assert statements, f"No quarterly statements in {sample_dir}"
    year = min(st.opening_date for st in statements).year
    period_to = date(year, 12, 31)

    tax_statement = MorganStanleyImporter(
        period_from=date(year, 1, 1),
        period_to=period_to,
        account_settings_list=[],
        strict_consistency=True,
    ).import_dir(sample_dir)

    year_end = max(statements, key=lambda st: st.closing_date)
    security = tax_statement.listOfSecurities.depot[0].security[0]
    closing = [
        s.quantity
        for s in security.stock
        if not s.mutation and s.referenceDate == period_to + timedelta(days=1)
    ]
    assert closing == [year_end.closing_shares]

    (cash_account,) = tax_statement.listOfBankAccounts.bankAccount
    assert cash_account.taxValue.balance == year_end.closing_cash
