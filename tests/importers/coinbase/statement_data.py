"""Synthetic Coinbase "Transaction History Report" data (made-up values only).

``statement_html`` mirrors the table layout of the real HTML report (header
table, Portfolio Summary with "as of" timestamps, transaction table);
``transactions_csv`` mirrors the CSV export (preamble + ``ID,Timestamp`` header).

The default year (``YEAR_2025``) reconciles: the derived opening balance is
0.01 BTC, and the year-end holdings equal opening plus all movements.
"""

from typing import Dict, List, Sequence, Tuple

ACCOUNT_ID = "0e1d2c3b-4a59-4687-9a01-23456789abcd"

Row = Tuple[str, str, str, str, str, str, str, str, str, str]
# (timestamp, type, asset, quantity, price currency, price, fees, subtotal, total, notes)

YEAR_2025: List[Row] = [
    (
        "2025-03-03 09:00:00 UTC",
        "Deposit",
        "EUR",
        "1000",
        "USD",
        "$1.10",
        "$0.00",
        "$1,100.00",
        "$1,100.00",
        "Deposit from EXAMPLE BANK (****0000)",
    ),
    (
        "2025-03-03 09:05:00 UTC",
        "Buy",
        "BTC",
        "0.02",
        "USD",
        "$55,000.00",
        "$0.00",
        "$1,100.00",
        "$1,100.00",
        "Bought 0.02 BTC for 1100 USD using EUR Wallet",
    ),
    (
        "2025-04-01 10:00:00 UTC",
        "Buy",
        "USDC",
        "500",
        "USD",
        "$1.00",
        "$0.00",
        "$500.00",
        "$500.00",
        "Bought 500 USDC for 500 USD using Visa ****0000",
    ),
    (
        "2025-05-01 12:00:00 UTC",
        "Reward Income",
        "USDC",
        "1.5",
        "USD",
        "$1.00",
        "$0.00",
        "$1.50",
        "$1.50",
        "Received 1.5 USDC from Coinbase Rewards",
    ),
    (
        "2025-06-01 12:00:00 UTC",
        "Reward Income",
        "USDC",
        "1.6",
        "USD",
        "$1.00",
        "$0.00",
        "$1.60",
        "$1.60",
        "Received 1.6 USDC from Coinbase Rewards",
    ),
    (
        "2025-07-01 08:00:00 UTC",
        "Convert",
        "USDC",
        "-200",
        "USD",
        "$1.00",
        "$1.00",
        "$199.00",
        "$200.00",
        "Converted 200 USDC to 0.003 BTC",
    ),
    (
        "2025-07-01 08:00:00 UTC",
        "Convert",
        "BTC",
        "0.003",
        "USD",
        "$66,333.33",
        "$0.00",
        "$199.00",
        "$199.00",
        "Converted 200 USDC to 0.003 BTC",
    ),
    (
        "2025-08-01 08:00:00 UTC",
        "Send",
        "BTC",
        "-0.001",
        "USD",
        "$70,000.00",
        "$0.00",
        "-$70.00",
        "-$70.00",
        "Sent 0.001 BTC to 0x0000000000000000000000000000000000000000",
    ),
    (
        "2025-09-01 08:00:00 UTC",
        "Receive",
        "USDT",
        "100",
        "USD",
        "$1.00",
        "$0.00",
        "$100.00",
        "$100.00",
        "Received 100 USDT from an external account",
    ),
]

HOLDINGS_2025: Dict[str, str] = {"BTC": "0.032", "USDC": "303.1", "USDT": "100"}


def statement_html(
    rows: Sequence[Row] = YEAR_2025,
    holdings: Dict[str, str] = HOLDINGS_2025,
    period_start: str = "2025-01-01 00:00:00 UTC",
    as_of: str = "2025-12-31 23:59:59 UTC",
    filter_text: str = "Type: all",
    account_id: str = ACCOUNT_ID,
) -> str:
    summary = "".join(
        f"<tr><td>{asset}</td><td>{qty}<span>as of {as_of}</span></td>"
        f"<td>1.00 USD/{asset}<span>as of {as_of}</span></td>"
        f"<td>1.00 USD<span>as of {as_of}</span></td></tr>"
        for asset, qty in holdings.items()
    )
    transactions = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    return (
        "<html><body><h1>Transaction History Report for Erika Mustermann</h1>"
        "<table><tr><th>Date Range</th><th>Filter</th><th>Account</th></tr>"
        f"<tr><td>From {period_start}</td><td>{filter_text}</td><td>erika@example.com</td></tr>"
        f"<tr><td>To {as_of}</td><td>Asset: all</td><td>{account_id}</td></tr></table>"
        "<h2>Portfolio Summary</h2>"
        "<table><tr><th>Asset</th><th>Quantity</th><th>Market Price</th><th>Market Value</th></tr>"
        f"{summary}<tr><td></td><td>Total Market Value 1.00 USD</td></tr></table>"
        "<table><tr><th>Timestamp</th><th>Transaction Type</th><th>Asset</th>"
        "<th>Quantity Transacted</th><th>Price Currency</th><th>Price at Transaction</th>"
        "<th>Fees</th><th>Subtotal</th><th>Total 1</th><th>Notes</th></tr>"
        f"{transactions}</table></body></html>"
    )


def transactions_csv(rows: Sequence[Row] = YEAR_2025) -> str:
    header = (
        "ID,Timestamp,Transaction Type,Asset,Quantity Transacted,Price Currency,"
        "Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),"
        "Fees and/or Spread,Notes,Sender Address,Recipient Address"
    )
    lines = [
        "",
        "Transactions",
        f"User,Erika Mustermann,{ACCOUNT_ID}",
        header,
    ]
    for index, (ts, kind, asset, qty, cur, price, fees, subtotal, total, notes) in enumerate(rows):
        values = [
            f"id{index}",
            ts,
            kind,
            asset,
            qty,
            cur,
            price,
            subtotal,
            total,
            fees,
            notes,
            "",
            "",
        ]
        lines.append(",".join(f'"{v}"' if "," in v else v for v in values))
    return "\n".join(lines) + "\n"
