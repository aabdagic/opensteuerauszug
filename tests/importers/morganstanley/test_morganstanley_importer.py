from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.config.models import MorganStanleyAccountSettings
from opensteuerauszug.importers.morganstanley.morganstanley_importer import (
    MorganStanleyImporter,
)
from opensteuerauszug.importers.morganstanley.statement_parser import parse_statement_pages

from .statement_text import Q1_2025, Q2_2025, Q3_2025, Q4_2025, statement_pages


def _year(*pages_list):
    return [parse_statement_pages(pages, source=f"q{i + 1}") for i, pages in enumerate(pages_list)]


def _import(statements, settings=None):
    importer = MorganStanleyImporter(
        period_from=date(2025, 1, 1),
        period_to=date(2025, 12, 31),
        account_settings_list=settings or [],
    )
    return importer.import_statements(statements)


def _only_security(statement):
    depots = statement.listOfSecurities.depot
    assert len(depots) == 1
    assert len(depots[0].security) == 1
    return depots[0].security[0]


def test_alphabet_class_c_is_identified_by_ticker_and_isin():
    security = _only_security(_import(_year(Q1_2025, Q2_2025, Q3_2025, Q4_2025)))
    assert security.symbol == "GOOG"
    assert security.isin == "US02079K1079"
    assert security.currency == "USD"


def test_vested_shares_and_sales_become_mutations_at_statement_prices():
    security = _only_security(_import(_year(Q1_2025, Q2_2025, Q3_2025, Q4_2025)))
    mutations = [(s.referenceDate, s.quantity, s.unitPrice) for s in security.stock if s.mutation]
    assert mutations == [
        (date(2025, 1, 25), Decimal("12.500"), Decimal("200.0000")),
        (date(2025, 2, 25), Decimal("8.000"), Decimal("180.0000")),
        (date(2025, 4, 25), Decimal("10.000"), Decimal("160.0000")),
        (date(2025, 8, 1), Decimal("-40.500"), Decimal("190.0000")),
    ]


def test_opening_and_closing_share_balances_come_from_statements():
    security = _only_security(_import(_year(Q1_2025, Q2_2025, Q3_2025, Q4_2025)))
    balances = {s.referenceDate: s.quantity for s in security.stock if not s.mutation}
    assert balances[date(2025, 1, 1)] == Decimal("10.000")
    assert balances[date(2026, 1, 1)] == Decimal("0.000")


def test_withholding_refund_is_netted_into_original_withholding():
    security = _only_security(_import(_year(Q1_2025, Q2_2025, Q3_2025, Q4_2025)))
    dividends = [p for p in security.payment if p.broker_label_original == "Dividend Credit"]
    withholding = [p for p in security.payment if p.broker_label_original == "Withholding Tax"]
    assert [(p.paymentDate, p.amount) for p in dividends] == [
        (date(2025, 3, 17), Decimal("6.10")),
        (date(2025, 6, 16), Decimal("8.51")),
    ]
    # 1.83 withheld in March, 0.92 refunded in April -> 0.91 net on the March dividend.
    assert [(p.paymentDate, p.amount) for p in withholding] == [
        (date(2025, 3, 17), Decimal("-0.91")),
        (date(2025, 6, 16), Decimal("-1.28")),
    ]
    assert withholding[0].nonRecoverableTaxAmountOriginal == Decimal("0.91")


def test_usd_cash_account_reports_year_end_balance():
    statement = _import(_year(Q1_2025, Q2_2025, Q3_2025, Q4_2025))
    accounts = statement.listOfBankAccounts.bankAccount
    assert len(accounts) == 1
    assert accounts[0].bankAccountCurrency == "USD"
    assert accounts[0].taxValue.balance == Decimal("0.00")


def test_missing_quarter_is_rejected():
    with pytest.raises(ValueError, match="Gap between statements"):
        _import(_year(Q1_2025, Q3_2025, Q4_2025))


def test_statements_ending_before_year_end_are_rejected():
    with pytest.raises(ValueError, match="before the tax period end"):
        _import(_year(Q1_2025, Q2_2025, Q3_2025))


def test_duplicate_statement_downloads_are_ignored():
    statements = _year(Q1_2025, Q2_2025, Q3_2025, Q4_2025)
    statements.append(parse_statement_pages(Q2_2025, source="q2 (1)"))
    security = _only_security(_import(statements))
    assert sum(1 for s in security.stock if s.mutation) == 4


def test_mismatched_quarter_boundaries_are_rejected():
    broken_q2 = list(Q2_2025)
    broken_q2[0] = broken_q2[0].replace("Number of Shares 30.500", "Number of Shares 31.500")
    with pytest.raises(ValueError, match="do not match opening balances"):
        _import(_year(Q1_2025, broken_q2, Q3_2025, Q4_2025))


def test_unknown_activity_stops_the_import():
    q1 = statement_pages(
        period="January 1 — March 31, 2025",
        opening="1/1/25",
        closing="3/31/25",
        shares=("10.000", "30.500"),
        cash=("$100.00", "$104.27"),
        rows=["2/1/25 Stock Split 0.000"],
    )
    with pytest.raises(NotImplementedError, match="Stock Split"):
        _import(_year(q1, Q2_2025, Q3_2025, Q4_2025))


def test_unknown_issuer_requires_symbol_in_config():
    q1 = [page.replace("ALPHABET INC CL C", "EXAMPLE CORP") for page in Q1_2025]
    statements = _year(q1, Q2_2025, Q3_2025, Q4_2025)
    with pytest.raises(ValueError, match="set 'symbol'"):
        _import(statements)

    settings = MorganStanleyAccountSettings(
        full_name="Erika Mustermann",
        account_number="MS00000001",
        broker_name="morganstanley",
        account_name_alias="plan",
        symbol="EXMP",
        isin="US0000000002",
    )
    security = _only_security(_import(statements, [settings]))
    assert security.symbol == "EXMP"
    assert security.isin == "US0000000002"
