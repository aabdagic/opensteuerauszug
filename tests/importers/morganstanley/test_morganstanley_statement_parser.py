from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.importers.morganstanley.statement_parser import (
    ActivityKind,
    parse_activity_line,
    parse_amount,
    parse_statement_pages,
)

from .statement_text import Q1_2025, Q3_2025, award_entry, statement_pages


def _q1_with_rows(rows):
    return statement_pages(
        period="January 1 — March 31, 2025",
        opening="1/1/25",
        closing="3/31/25",
        shares=("0.000", "0.000"),
        cash=("$0.00", "$0.00"),
        rows=rows,
    )


@pytest.mark.parametrize(
    "token, expected",
    [
        ("$1,234.56", Decimal("1234.56")),
        ("(12.345)", Decimal("-12.345")),
        ("$(9,876.54)", Decimal("-9876.54")),
        ("100.0000", Decimal("100.0000")),
    ],
)
def test_statement_amounts_treat_parentheses_as_negative(token, expected):
    assert parse_amount(token) == expected


def test_share_release_row_has_quantity_and_price():
    row = parse_activity_line("1/25/25 Release 12.500 $200.0000  ")
    assert row.kind == ActivityKind.RELEASE
    assert row.trade_date == date(2025, 1, 25)
    assert row.numbers == (Decimal("12.500"), Decimal("200.0000"))


def test_cash_release_row_has_single_amount():
    row = parse_activity_line("10/25/25 Release 2,000.00")
    assert row.kind == ActivityKind.RELEASE
    assert row.numbers == (Decimal("2000.00"),)


def test_multi_word_activities_are_classified():
    assert parse_activity_line("4/29/25 Cancel Withholding Tax $0.92").kind == (
        ActivityKind.WITHHOLDING_CANCEL
    )
    assert parse_activity_line("8/4/25 Proceeds Disbursement (7,807.42)").kind == (
        ActivityKind.DISBURSEMENT
    )
    assert parse_activity_line("6/16/25 Dividend Credit $8.51 8.51").kind == (ActivityKind.DIVIDEND)


def test_unrecognised_activity_is_flagged_unknown():
    row = parse_activity_line("5/1/25 Stock Split 10.000")
    assert row.kind == ActivityKind.UNKNOWN
    assert row.activity == "Stock Split"


def test_undated_lines_are_not_activity_rows():
    assert parse_activity_line("Transaction Date Activity Type Quantity Price") is None


def test_summary_balances_and_dates_are_parsed():
    st = parse_statement_pages(Q1_2025, source="q1")
    assert st.account_number == "MS00000001"
    assert st.issuer_description == "ALPHABET INC CL C"
    assert st.holder_name == "Erika Mustermann"
    assert (st.opening_date, st.closing_date) == (date(2025, 1, 1), date(2025, 3, 31))
    assert (st.opening_shares, st.closing_shares) == (Decimal("10.000"), Decimal("30.500"))
    assert (st.opening_cash, st.closing_cash) == (Decimal("100.00"), Decimal("104.27"))
    assert st.closing_share_price == Decimal("110.0000")


def test_award_activity_section_is_not_read_as_transactions():
    st = parse_statement_pages(Q3_2025, source="q3")
    assert [row.kind for row in st.lines] == [
        ActivityKind.SALE,
        ActivityKind.DISBURSEMENT,
        ActivityKind.RELEASE,
    ]


def test_award_entries_are_parsed_with_price_shares_and_amounts():
    st = parse_statement_pages(Q3_2025, source="q3")
    (award,) = st.awards
    assert (award.activity_date, award.settlement_date) == (date(2025, 8, 25), date(2025, 8, 27))
    assert (award.activity, award.grant_id, award.award_type) == ("Release", "C1000001", "RST")
    assert (award.price, award.shares, award.net_shares) == (
        Decimal("200.0000"),
        Decimal("10.000"),
        Decimal("10.000"),
    )
    assert (award.gross, award.net) == (Decimal("2000.00"), Decimal("1500.00"))


def test_transaction_row_split_by_text_extraction_is_rejected():
    pages = _q1_with_rows(["8/1/25", "Sale (40.500) 190.0000 $7,695.00 $7,695.00"])
    with pytest.raises(ValueError, match="without activity or amounts"):
        parse_statement_pages(pages, source="q1")


def test_dated_row_outside_known_sections_is_rejected():
    pages = _q1_with_rows([])
    pages[2] += "INTEREST AND OTHER INCOME\n3/31/25 Interest Credit $1.23\n"
    with pytest.raises(ValueError, match="outside the known statement sections"):
        parse_statement_pages(pages, source="q1")


def test_truncated_award_entry_is_rejected():
    pages = _q1_with_rows([])
    entry = award_entry("2/25/25", "2/27/25", "C1", "$1.0000", "1.000", "$1.00 $1.00")
    pages[2] += "STOCK OPTION AND AWARD ACTIVITY\n" + "\n".join(entry.splitlines()[:5]) + "\n"
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_statement_pages(pages, source="q1")


def test_nonzero_unsettled_cash_is_rejected():
    pages = statement_pages(
        period="January 1 — March 31, 2025",
        opening="1/1/25",
        closing="3/31/25",
        shares=("0.000", "0.000"),
        cash=("$0.00", "$0.00"),
        rows=[],
    )
    pages[0] = pages[0].replace("Net Unsettled Cash $0.00 $0.00", "Net Unsettled Cash $0.00 $5.00")
    with pytest.raises(NotImplementedError):
        parse_statement_pages(pages, source="q1")


def test_non_statement_text_is_rejected():
    with pytest.raises(ValueError):
        parse_statement_pages(["Form 1042-S"], source="1042s")
