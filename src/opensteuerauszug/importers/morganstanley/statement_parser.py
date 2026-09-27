"""Parse Morgan Stanley at Work (Shareworks) quarterly statement PDFs.

The quarterly "STATEMENT For the Period ..." PDFs issued by Morgan Stanley
Smith Barney for stock plan accounts are the only export that carries
USD-denominated amounts together with dividends, withholding tax and the
quarter-end share and cash balances.  (The Activity Report XLSX converts
every amount with a single display rate, so it is unsuitable for tax use.)

Only two parts of the statement are used:

* the first-page "Share Purchase and Holdings Summary" (opening/closing
  share count, share price and cash value), and
* the "SHARE PURCHASE AND HOLDINGS" transaction list, which may continue
  over several pages.

The "STOCK OPTION AND AWARD ACTIVITY" section repeats the vesting events
with payroll-tax detail and is deliberately ignored to avoid double
counting; vest income is reported on the Swiss salary certificate.

The parser works on the text of each page so it can be unit tested
without real PDFs.  See ``docs/importer_morganstanley.md`` for the line
formats.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum, auto
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class ActivityKind(Enum):
    RELEASE = auto()
    SALE = auto()
    DIVIDEND = auto()
    WITHHOLDING = auto()
    WITHHOLDING_CANCEL = auto()
    DISBURSEMENT = auto()
    UNKNOWN = auto()


_ACTIVITY_KINDS = {
    "release": ActivityKind.RELEASE,
    "sale": ActivityKind.SALE,
    "dividend credit": ActivityKind.DIVIDEND,
    "withholding tax": ActivityKind.WITHHOLDING,
    "cancel withholding tax": ActivityKind.WITHHOLDING_CANCEL,
    "proceeds disbursement": ActivityKind.DISBURSEMENT,
}

_SECTION_START = "SHARE PURCHASE AND HOLDINGS"
_SECTION_END_PREFIXES = ("STOCK OPTION AND AWARD ACTIVITY", "Sell Transactions are provided")

_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")
_NUMBER_RE = re.compile(r"^\$?\(?\$?\d[\d,]*(?:\.\d+)?\)?$")
_AS_OF_RE = re.compile(r"\(as of (\d{1,2}/\d{1,2}/\d{2})\)")
_ACCOUNT_RE = re.compile(r"Account Number:\s*(\S+)")
_ISSUER_RE = re.compile(r"Issuer Description:\s*(.+)")
_BARCODE_RE = re.compile(r"^\*.*\*$")


def _summary_pair_re(label: str) -> re.Pattern:
    token = r"(\$?\(?\$?\d[\d,]*(?:\.\d+)?\)?)"
    return re.compile(rf"^{re.escape(label)}\s+{token}\s+{token}\s*$", re.MULTILINE)


_SHARES_RE = _summary_pair_re("Number of Shares")
_PRICE_RE = _summary_pair_re("Share Price")
_CASH_RE = _summary_pair_re("Cash Value")
_UNSETTLED_RE = _summary_pair_re("Net Unsettled Cash")


@dataclass(frozen=True)
class StatementLine:
    """One row of the SHARE PURCHASE AND HOLDINGS list."""

    trade_date: date
    kind: ActivityKind
    activity: str
    numbers: Tuple[Decimal, ...]
    text: str


@dataclass
class QuarterlyStatement:
    source: str
    account_number: str
    issuer_description: str
    holder_name: Optional[str]
    opening_date: date
    closing_date: date
    opening_shares: Decimal
    closing_shares: Decimal
    opening_cash: Decimal
    closing_cash: Decimal
    opening_share_price: Optional[Decimal]
    closing_share_price: Optional[Decimal]
    lines: List[StatementLine] = field(default_factory=list)


def parse_us_short_date(text: str) -> date:
    """Parse ``M/D/YY`` as used throughout the statements."""
    m = _DATE_RE.match(text.strip())
    if not m:
        raise ValueError(f"Not a M/D/YY date: {text!r}")
    month, day, year = (int(g) for g in m.groups())
    return date(2000 + year, month, day)


def parse_amount(token: str) -> Decimal:
    """Parse ``$1,234.56``, ``(12.345)`` or ``$(9,876.54)``; parentheses mean negative."""
    raw = token.strip()
    if not _NUMBER_RE.match(raw):
        raise ValueError(f"Not a statement amount: {token!r}")
    negative = "(" in raw
    value = Decimal(raw.replace("$", "").replace(",", "").replace("(", "").replace(")", ""))
    return -value if negative else value


def is_quarterly_statement(first_page_text: str) -> bool:
    return (
        "Morgan Stanley" in first_page_text
        and "Share Purchase and Holdings Summary" in first_page_text
    )


def parse_activity_line(line: str) -> Optional[StatementLine]:
    """Parse one transaction row, or return None if *line* is not a dated row."""
    tokens = line.split()
    if len(tokens) < 2 or not _DATE_RE.match(tokens[0]):
        return None
    trade_date = parse_us_short_date(tokens[0])

    words: List[str] = []
    idx = 1
    while idx < len(tokens) and not _NUMBER_RE.match(tokens[idx]):
        words.append(tokens[idx])
        idx += 1
    number_tokens = tokens[idx:]
    stray = [t for t in number_tokens if not _NUMBER_RE.match(t)]
    if stray or not words:
        raise ValueError(f"Cannot parse statement row {line!r}")

    activity = " ".join(words)
    kind = _ACTIVITY_KINDS.get(activity.lower(), ActivityKind.UNKNOWN)
    numbers = tuple(parse_amount(t) for t in number_tokens)
    return StatementLine(
        trade_date=trade_date,
        kind=kind,
        activity=activity,
        numbers=numbers,
        text=line.strip(),
    )


def _extract_activity_lines(pages: Sequence[str]) -> List[StatementLine]:
    rows: List[StatementLine] = []
    in_section = False
    for page in pages:
        for raw in page.splitlines():
            line = raw.strip()
            if line.startswith(_SECTION_START):
                in_section = True
                continue
            if line.startswith(_SECTION_END_PREFIXES):
                in_section = False
                continue
            if not in_section:
                continue
            row = parse_activity_line(line)
            if row is not None:
                rows.append(row)
    return rows


def _holder_name(first_page: str) -> Optional[str]:
    lines = [ln.strip() for ln in first_page.splitlines()]
    for i, ln in enumerate(lines):
        if _BARCODE_RE.match(ln) and i + 1 < len(lines) and lines[i + 1]:
            return lines[i + 1]
    return None


def _require(pattern: re.Pattern, text: str, what: str, source: str) -> re.Match:
    m = pattern.search(text)
    if not m:
        raise ValueError(f"{source}: could not find {what} in statement summary")
    return m


def parse_statement_pages(pages: Sequence[str], source: str) -> QuarterlyStatement:
    """Build a :class:`QuarterlyStatement` from the extracted text of each page."""
    if not pages or not is_quarterly_statement(pages[0]):
        raise ValueError(f"{source}: not a Morgan Stanley quarterly statement")
    first = pages[0]

    as_of = _AS_OF_RE.findall(first)
    if len(as_of) < 2:
        raise ValueError(f"{source}: could not find opening/closing '(as of ...)' dates")
    opening_date = parse_us_short_date(as_of[0])
    closing_date = parse_us_short_date(as_of[1])

    shares = _require(_SHARES_RE, first, "'Number of Shares'", source)
    cash = _require(_CASH_RE, first, "'Cash Value'", source)
    price = _PRICE_RE.search(first)
    unsettled = _UNSETTLED_RE.search(first)
    if unsettled and any(parse_amount(g) != 0 for g in unsettled.groups()):
        # Never observed so far; the reconciliation below would not know
        # which side of the quarter boundary this cash belongs to.
        raise NotImplementedError(
            f"{source}: non-zero 'Net Unsettled Cash' ({unsettled.group(0)!r}) is not "
            "supported yet. Please report this so support can be added."
        )

    account = _require(_ACCOUNT_RE, first, "'Account Number'", source).group(1)
    issuer = _require(_ISSUER_RE, first, "'Issuer Description'", source).group(1).strip()

    return QuarterlyStatement(
        source=source,
        account_number=account,
        issuer_description=issuer,
        holder_name=_holder_name(first),
        opening_date=opening_date,
        closing_date=closing_date,
        opening_shares=parse_amount(shares.group(1)),
        closing_shares=parse_amount(shares.group(2)),
        opening_cash=parse_amount(cash.group(1)),
        closing_cash=parse_amount(cash.group(2)),
        opening_share_price=parse_amount(price.group(1)) if price else None,
        closing_share_price=parse_amount(price.group(2)) if price else None,
        lines=_extract_activity_lines(pages),
    )


def extract_pdf_pages(path: str) -> List[str]:
    from pypdf import PdfReader

    # The statement fonts trigger harmless "Advanced encoding /NULL not
    # implemented yet" errors from pypdf's text extraction.
    logging.getLogger("pypdf._cmap").setLevel(logging.CRITICAL)
    reader = PdfReader(path)
    return [page.extract_text() or "" for page in reader.pages]


def load_statement_pdf(path: str) -> Optional[QuarterlyStatement]:
    """Parse *path*, or return None if the PDF is not a quarterly statement."""
    pages = extract_pdf_pages(path)
    if not pages or not is_quarterly_statement(pages[0]):
        logger.info("Skipping %s: not a Morgan Stanley quarterly statement", path)
        return None
    return parse_statement_pages(pages, source=path)
