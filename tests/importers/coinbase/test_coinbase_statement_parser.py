from datetime import datetime, timezone
from decimal import Decimal

import pytest

from opensteuerauszug.importers.coinbase.statement_parser import (
    matching_keys,
    parse_money,
    parse_statement_html,
    parse_transactions_csv,
)

from .statement_data import statement_html, transactions_csv


def _statement(**kwargs):
    return parse_statement_html(statement_html(**kwargs).encode(), "statement.htm")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("$1,100.00", Decimal("1100.00")),
        ("-$70.00", Decimal("-70.00")),
        ("1.00 USD", Decimal("1.00")),
        ("", None),
    ],
)
def test_money_values_accept_dollar_signs_minus_and_currency_suffix(text, expected):
    assert parse_money(text, "test") == expected


def test_statement_header_and_holdings_are_parsed():
    st = _statement()
    assert st.period_start == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert st.holdings_as_of == datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert st.filter == "Type: all"
    assert st.holdings == {
        "BTC": Decimal("0.032"),
        "USDC": Decimal("303.1"),
        "USDT": Decimal("100"),
    }


def test_statement_transactions_keep_signs_and_values():
    st = _statement()
    assert len(st.transactions) == 9
    send = next(t for t in st.transactions if t.kind == "Send")
    assert (send.asset, send.quantity, send.subtotal) == (
        "BTC",
        Decimal("-0.001"),
        Decimal("-70.00"),
    )
    reward = next(t for t in st.transactions if t.kind == "Reward Income")
    assert (reward.quantity, reward.subtotal, reward.price_currency) == (
        Decimal("1.5"),
        Decimal("1.50"),
        "USD",
    )


def test_csv_export_matches_the_statement_transactions():
    rows = parse_transactions_csv(transactions_csv(), "export.csv")
    assert matching_keys(rows, _statement().transactions)


def test_non_statement_html_is_rejected():
    with pytest.raises(ValueError, match="not a Coinbase transaction history report"):
        parse_statement_html(b"<html><body>hello</body></html>", "other.htm")


def test_holding_without_as_of_timestamp_is_rejected():
    html = statement_html().replace(
        "<span>as of 2025-12-31 23:59:59 UTC</span></td><td>1.00 USD/BTC", "</td><td>1.00 USD/BTC"
    )
    with pytest.raises(ValueError, match="cannot read holding"):
        parse_statement_html(html.encode(), "statement.htm")


def test_csv_without_header_is_rejected():
    with pytest.raises(ValueError, match="no 'ID,Timestamp' header"):
        parse_transactions_csv("a,b\n1,2\n", "other.csv")
