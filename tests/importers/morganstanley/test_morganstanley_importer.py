from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.config.models import MorganStanleyAccountSettings
from opensteuerauszug.importers.morganstanley.morganstanley_importer import (
    MorganStanleyImporter,
)
from opensteuerauszug.importers.morganstanley.statement_parser import parse_statement_pages

from .statement_text import Q1_2025, Q2_2025, Q3_2025, Q4_2025, award_entry, statement_pages


def _year(*pages_list):
    return [parse_statement_pages(pages, source=f"q{i + 1}") for i, pages in enumerate(pages_list)]


def _import(statements, settings=None, strict=True):
    importer = MorganStanleyImporter(
        period_from=date(2025, 1, 1),
        period_to=date(2025, 12, 31),
        account_settings_list=settings or [],
        strict_consistency=strict,
    )
    return importer.import_statements(statements)


def _only_security(statement):
    depots = statement.listOfSecurities.depot
    assert len(depots) == 1
    assert len(depots[0].security) == 1
    return depots[0].security[0]


def _withholding(security):
    return {
        p.paymentDate: p.amount
        for p in security.payment
        if p.broker_label_original == "Withholding Tax"
    }


def _settings(account, **extra):
    return MorganStanleyAccountSettings(
        full_name="Erika Mustermann",
        account_number=account,
        broker_name="morganstanley",
        account_name_alias="plan",
        **extra,
    )


def _q2(rows, cash_close="$112.42"):
    return statement_pages(
        period="April 1 — June 30, 2025",
        opening="4/1/25",
        closing="6/30/25",
        shares=("30.500", "40.500"),
        cash=("$104.27", cash_close),
        rows=rows,
    )


FULL_YEAR = (Q1_2025, Q2_2025, Q3_2025, Q4_2025)


# --- identification -----------------------------------------------------


def test_alphabet_class_c_is_identified_by_ticker_and_isin():
    security = _only_security(_import(_year(*FULL_YEAR)))
    assert security.symbol == "GOOG"
    assert security.isin == "US02079K1079"
    assert security.currency == "USD"


def test_unknown_issuer_requires_symbol_in_config():
    q1 = [page.replace("ALPHABET INC CL C", "EXAMPLE CORP") for page in Q1_2025]
    others = [[p.replace("ALPHABET INC CL C", "EXAMPLE CORP") for p in q] for q in FULL_YEAR[1:]]
    statements = _year(q1, *others)
    with pytest.raises(ValueError, match="set 'symbol'"):
        _import(statements)

    settings = _settings("MS00000001", symbol="EXMP", isin="US0000000002")
    security = _only_security(_import(statements, [settings]))
    assert security.symbol == "EXMP"
    assert security.isin == "US0000000002"


def test_settings_of_another_account_are_not_applied():
    other = _settings("MS99999999", symbol="NESN", isin="CH0038863350")
    security = _only_security(_import(_year(*FULL_YEAR), [other]))
    assert (security.symbol, security.isin) == ("GOOG", "US02079K1079")


def test_statements_naming_different_securities_are_rejected():
    q3 = [p.replace("ALPHABET INC CL C", "ALPHABET INC CL A") for p in Q3_2025]
    with pytest.raises(ValueError, match="different securities"):
        _import(_year(Q1_2025, Q2_2025, q3, Q4_2025))


# --- shares and cash ----------------------------------------------------


def test_vests_and_sales_become_mutations_at_statement_prices():
    security = _only_security(_import(_year(*FULL_YEAR)))
    mutations = [
        (s.referenceDate, s.quantity, s.unitPrice, s.name) for s in security.stock if s.mutation
    ]
    assert mutations == [
        (date(2025, 1, 25), Decimal("12.500"), Decimal("200.0000"), "Release (vesting)"),
        (date(2025, 2, 25), Decimal("8.000"), Decimal("180.0000"), "Release (vesting)"),
        (date(2025, 4, 25), Decimal("10.000"), Decimal("160.0000"), "Release (vesting)"),
        (date(2025, 8, 1), Decimal("-40.500"), Decimal("190.0000"), "Sale"),
        (date(2025, 8, 25), Decimal("10.000"), Decimal("200.0000"), "Release (sold at vest)"),
        (date(2025, 8, 25), Decimal("-10.000"), Decimal("200.0000"), "Sale at vest"),
        (date(2025, 10, 25), Decimal("12.000"), Decimal("250.0000"), "Release (sold at vest)"),
        (date(2025, 10, 25), Decimal("-12.000"), Decimal("250.0000"), "Sale at vest"),
    ]


def test_opening_and_closing_share_balances_come_from_statements():
    security = _only_security(_import(_year(*FULL_YEAR)))
    balances = {s.referenceDate: s.quantity for s in security.stock if not s.mutation}
    assert balances[date(2025, 1, 1)] == Decimal("10.000")
    assert balances[date(2026, 1, 1)] == Decimal("0.000")


def test_usd_cash_account_reports_year_end_balance():
    statement = _import(_year(*FULL_YEAR))
    accounts = statement.listOfBankAccounts.bankAccount
    assert len(accounts) == 1
    assert accounts[0].bankAccountCurrency == "USD"
    assert accounts[0].taxValue.balance == Decimal("0.00")


def test_cash_release_without_award_entry_is_rejected():
    q3 = statement_pages(
        period="July 1 — September 30, 2025",
        opening="7/1/25",
        closing="9/30/25",
        shares=("40.500", "0.000"),
        cash=("$112.42", "$1,500.00"),
        rows=[
            "8/1/25 Sale (40.500) 190.0000 $7,695.00 $7,695.00",
            "8/4/25 Proceeds Disbursement (7,807.42)",
            "8/25/25 Release 1,500.00",
        ],
    )
    with pytest.raises(ValueError, match="cannot find the award entry"):
        _import(_year(Q1_2025, Q2_2025, q3, Q4_2025))


def test_award_whose_amounts_do_not_add_up_is_rejected():
    q3 = [p.replace("$2,000.00 $500.00 $1,500.00", "$2,000.00 $400.00 $1,500.00") for p in Q3_2025]
    with pytest.raises(ValueError, match="minus fees and taxes"):
        _import(_year(Q1_2025, Q2_2025, q3, Q4_2025))


def test_missing_quarter_is_rejected():
    with pytest.raises(ValueError, match="Gap between statements"):
        _import(_year(Q1_2025, Q3_2025, Q4_2025))


def test_statements_ending_before_year_end_are_rejected():
    with pytest.raises(ValueError, match="before the tax period end"):
        _import(_year(Q1_2025, Q2_2025, Q3_2025))


def test_mismatched_quarter_boundaries_are_rejected():
    broken_q2 = list(Q2_2025)
    broken_q2[0] = broken_q2[0].replace("Number of Shares 30.500", "Number of Shares 31.500")
    with pytest.raises(ValueError, match="do not match opening balances"):
        _import(_year(Q1_2025, broken_q2, Q3_2025, Q4_2025))


def test_identical_duplicate_downloads_are_ignored():
    statements = _year(*FULL_YEAR)
    statements.append(parse_statement_pages(Q2_2025, source="q2 (1)"))
    security = _only_security(_import(statements))
    assert sum(1 for s in security.stock if s.mutation) == 8


def test_differing_copies_of_the_same_quarter_are_rejected():
    corrected = [p.replace("(1.28)", "(1.27)").replace("$112.42", "$112.43") for p in Q2_2025]
    statements = _year(*FULL_YEAR)
    statements.append(parse_statement_pages(corrected, source="q2 corrected"))
    with pytest.raises(ValueError, match="cover the same period .* but differ"):
        _import(statements)


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


# --- dividends and withholding -------------------------------------------


def test_withholding_refund_is_netted_into_the_dividend_it_belongs_to():
    security = _only_security(_import(_year(*FULL_YEAR)))
    dividends = [p for p in security.payment if p.broker_label_original == "Dividend Credit"]
    assert [(p.paymentDate, p.amount) for p in dividends] == [
        (date(2025, 3, 17), Decimal("6.10")),
        (date(2025, 6, 16), Decimal("8.51")),
    ]
    # 1.83 withheld in March, 0.92 refunded in April -> 0.91 (15%) on the March dividend.
    assert _withholding(security) == {
        date(2025, 3, 17): Decimal("-0.91"),
        date(2025, 6, 16): Decimal("-1.28"),
    }
    march = next(p for p in security.payment if p.amount == Decimal("-0.91"))
    assert march.nonRecoverableTaxAmountOriginal == Decimal("0.91")


def test_late_refund_is_netted_into_its_own_dividend_not_the_latest():
    q2 = _q2(
        [
            "4/25/25 Release 10.000 $160.0000  ",
            "6/16/25 Dividend Credit $8.51 8.51",
            "6/16/25 Withholding Tax (1.28)",
            "6/20/25 Cancel Withholding Tax $0.92",
        ]
    )
    security = _only_security(_import(_year(Q1_2025, q2, Q3_2025, Q4_2025)))
    assert _withholding(security) == {
        date(2025, 3, 17): Decimal("-0.91"),
        date(2025, 6, 16): Decimal("-1.28"),
    }


def test_refund_matching_no_withholding_is_rejected():
    q2 = _q2(
        [
            "4/25/25 Release 10.000 $160.0000  ",
            "4/29/25 Cancel Withholding Tax $0.50",
            "6/16/25 Dividend Credit $8.51 8.51",
            "6/16/25 Withholding Tax (1.28)",
        ],
    )
    # Checked before the cash reconciliation, so the quarter's cash need not add up.
    with pytest.raises(ValueError, match="does not bring any earlier withholding down"):
        _import(_year(Q1_2025, q2, Q3_2025, Q4_2025))


def test_refund_booked_in_the_following_year_reaches_last_years_dividend():
    q4 = statement_pages(
        period="October 1 — December 31, 2025",
        opening="10/1/25",
        closing="12/31/25",
        shares=("0.000", "20.000"),
        cash=("$1,500.00", "$2.94"),
        rows=[
            "10/1/25 Proceeds Disbursement $(1,500.00)",
            "10/25/25 Release 2,000.00",
            "12/1/25 Release 20.000 $300.0000  ",
            "12/15/25 Dividend Credit $4.20 $4.20",
            "12/15/25 Withholding Tax (1.26)",
            "12/30/25 Proceeds Disbursement (1,800.00)",
            "12/30/25 Proceeds Disbursement (200.00)",
        ],
        awards=[
            award_entry(
                "10/25/25",
                "10/29/25",
                "C1000001",
                "$250.0000",
                "12.000",
                "$3,000.00 $1,000.00 $2,000.00",
            )
        ],
    )
    q1_2026 = statement_pages(
        period="January 1 — March 31, 2026",
        opening="1/1/26",
        closing="3/31/26",
        shares=("20.000", "20.000"),
        cash=("$2.94", "$3.57"),
        rows=["1/20/26 Cancel Withholding Tax $0.63"],
    )
    statements = _year(Q1_2025, Q2_2025, Q3_2025, q4, q1_2026)
    statement = _import(statements)
    # 1.26 (30%) withheld in December, 0.63 refunded in January 2026 -> 0.63 (15%).
    assert _withholding(_only_security(statement))[date(2025, 12, 15)] == Decimal("-0.63")
    # The refund is 2026 cash, not part of the 2025 year-end balance.
    assert statement.listOfBankAccounts.bankAccount[0].taxValue.balance == Decimal("2.94")


def test_withholding_without_dividend_is_rejected():
    q2 = _q2(
        [
            "4/25/25 Release 10.000 $160.0000  ",
            "5/2/25 Withholding Tax (0.50)",
            "6/16/25 Dividend Credit $8.51 8.51",
            "6/16/25 Withholding Tax (1.28)",
        ],
    )
    with pytest.raises(NotImplementedError, match="without a dividend"):
        _import(_year(Q1_2025, q2, Q3_2025, Q4_2025))
