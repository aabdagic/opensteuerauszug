"""Parse Coinbase "Transaction History Report" statements (HTML) and CSV exports.

Coinbase (Statements page, "Generate custom statement") produces the same
report as HTML, PDF or CSV.  The HTML version is the importer's main input
because it states its date range and filter and contains the "Portfolio
Summary" (holdings at the end of the period) in addition to the full
transaction list.  The CSV export has the same transactions (plus IDs and
wallet addresses) but no period or holdings; when present it is used as a
cross-check.

HTML layout (tables identified by their header row):

* ``Date Range | Filter | Account`` -- e.g. "From 2025-01-01 00:00:00 UTC",
  "Type: all", the account e-mail; the next row has the account ID (a UUID)
  under the e-mail;
* ``Asset | Quantity | Market Price | Market Value`` -- one row per asset with
  a non-zero balance; the quantity cell reads "0.5as of 2025-12-31 23:59:59 UTC";
  a final row holds the total market value;
* ``Timestamp | Transaction Type | Asset | Quantity Transacted | Price
  Currency | Price at Transaction | Fees | Subtotal | Total | Notes``.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S UTC"
_AS_OF_RE = re.compile(r"as of (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)")
_FROM_RE = re.compile(r"From (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)")
_LEADING_NUMBER_RE = re.compile(r"^-?[\d,]*\.?\d+")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")


@dataclass(frozen=True)
class Transaction:
    timestamp: datetime
    kind: str
    asset: str
    quantity: Decimal
    price_currency: str
    price: Optional[Decimal]
    fees: Optional[Decimal]
    subtotal: Optional[Decimal]
    total: Optional[Decimal]
    notes: str

    def key(self) -> tuple:
        """Identity used to compare the HTML statement with a CSV export."""
        return (self.timestamp, self.kind, self.asset, self.quantity)


@dataclass
class Statement:
    source: str
    account: str
    filter: str
    period_start: datetime
    holdings_as_of: Optional[datetime]
    account_id: Optional[str] = None
    holdings: Dict[str, Decimal] = field(default_factory=dict)
    transactions: List[Transaction] = field(default_factory=list)


def parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text.strip(), _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)


def parse_quantity(text: str, context: str) -> Decimal:
    raw = text.strip().replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{context}: invalid quantity {text!r}") from None


def parse_money(text: str, context: str) -> Optional[Decimal]:
    """Parse ``$1,234.56``, ``-$0.50``, ``1234.56 USD``; empty means no value."""
    raw = text.strip()
    if not raw:
        return None
    cleaned = re.sub(r"[A-Z]{3}$", "", raw).strip().replace("$", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{context}: invalid amount {text!r}") from None


def _transaction(values: Dict[str, str], context: str) -> Transaction:
    def col(*names: str) -> str:
        for name in names:
            if name in values:
                return values[name]
        raise ValueError(f"{context}: missing column {names[0]!r}")

    return Transaction(
        timestamp=parse_timestamp(col("Timestamp")),
        kind=col("Transaction Type").strip(),
        asset=col("Asset").strip(),
        quantity=parse_quantity(col("Quantity Transacted"), context),
        price_currency=col("Price Currency").strip(),
        price=parse_money(col("Price at Transaction"), context),
        fees=parse_money(col("Fees", "Fees and/or Spread"), context),
        subtotal=parse_money(col("Subtotal"), context),
        total=parse_money(
            col("Total", "Total 1", "Total (inclusive of fees and/or spread)"), context
        ),
        notes=col("Notes").strip(),
    )


def _tables(doc) -> List[List[List[str]]]:
    tables = []
    for table in doc.iter("table"):
        rows = [[cell.text_content().strip() for cell in row] for row in table.iter("tr")]
        if rows:
            tables.append(rows)
    return tables


def is_statement_html(text: str) -> bool:
    return "Transaction History Report" in text and "Portfolio Summary" in text


def parse_statement_html(content: bytes, source: str) -> Statement:
    from lxml import html

    doc = html.fromstring(content)
    if not is_statement_html(doc.text_content()):
        raise ValueError(f"{source}: not a Coinbase transaction history report")
    tables = _tables(doc)

    header = next((t for t in tables if t[0][:1] == ["Date Range"]), None)
    if header is None or len(header) < 2:
        raise ValueError(f"{source}: statement header (Date Range / Filter) not found")
    head = dict(zip(header[0], header[1]))
    period = _FROM_RE.search(head.get("Date Range", ""))
    if period is None:
        raise ValueError(f"{source}: cannot read the statement's date range")
    # The account column holds the e-mail and, in the next row, the account ID.
    column = header[0].index("Account") if "Account" in header[0] else None
    account_cells = [row[column] for row in header[1:] if column is not None and column < len(row)]
    account_ids = {m.group(0).lower() for cell in account_cells for m in _UUID_RE.finditer(cell)}
    if len(account_ids) > 1:
        raise ValueError(f"{source}: several account IDs in the statement header")

    statement = Statement(
        source=source,
        account=head.get("Account", ""),
        filter=head.get("Filter", ""),
        period_start=parse_timestamp(period.group(1)),
        holdings_as_of=None,
        account_id=account_ids.pop() if account_ids else None,
    )

    summary = next((t for t in tables if t[0][:2] == ["Asset", "Quantity"]), None)
    if summary is None:
        raise ValueError(f"{source}: Portfolio Summary table not found")
    for row in summary[1:]:
        if not row or not row[0]:
            continue  # total market value row
        asset, quantity_cell = row[0], row[1]
        number = _LEADING_NUMBER_RE.match(quantity_cell)
        as_of = _AS_OF_RE.search(quantity_cell)
        if number is None or as_of is None:
            raise ValueError(f"{source}: cannot read holding {row!r}")
        timestamp = parse_timestamp(as_of.group(1))
        if statement.holdings_as_of not in (None, timestamp):
            raise ValueError(f"{source}: holdings have different 'as of' times")
        statement.holdings_as_of = timestamp
        if asset in statement.holdings:
            raise ValueError(f"{source}: asset {asset} listed twice in the Portfolio Summary")
        statement.holdings[asset] = parse_quantity(number.group(0), source)

    for table in tables:
        if "Transaction Type" not in table[0]:
            continue
        columns = table[0]
        for index, row in enumerate(table[1:], start=2):
            if not any(row):
                continue
            if len(row) != len(columns):
                raise ValueError(f"{source}: transaction row {index} has {len(row)} cells")
            statement.transactions.append(
                _transaction(dict(zip(columns, row)), f"{source}, row {index}")
            )
    return statement


def parse_transactions_csv(text: str, source: str) -> List[Transaction]:
    """Parse the CSV export (a short preamble, then a header starting with ``ID,Timestamp``)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("ID,Timestamp")), None)
    if start is None:
        raise ValueError(f"{source}: not a Coinbase transaction CSV (no 'ID,Timestamp' header)")
    return [
        _transaction(row, f"{source}, line {start + index + 2}")
        for index, row in enumerate(csv.DictReader(lines[start:]))
    ]


def matching_keys(a: Sequence[Transaction], b: Sequence[Transaction]) -> bool:
    return sorted(t.key() for t in a) == sorted(t.key() for t in b)
