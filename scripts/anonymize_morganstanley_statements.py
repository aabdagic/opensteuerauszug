"""Anonymise Morgan Stanley at Work quarterly statement PDFs for use as test samples.

Takes the quarterly statements of one tax year and rewrites the text in the
PDFs themselves, so the layout, fonts and encodings stay those of the real
documents while everything personal is replaced:

* Name, address, mailing code, account number and grant IDs are replaced with
  fixed fake values.
* Share quantities are replaced with new random values: one random scale per
  grant (sell-at-vest releases) or per cluster of similar lot sizes (share
  releases), plus a small per-row jitter.  Independent factors remove the
  integer grant structure that a single global factor would preserve.
* Everything derived is recomputed from the new quantities: payroll taxes (with
  a new random rate per vest date), proceeds, sales (matched to the lots they
  sell), dividends (holdings x the per-share dividend, so they still match the
  Kursliste), withholding and refunds, disbursements and all balances.  The
  statements keep reconciling exactly.
* Dates and prices (public market data) are kept.

The random values come from ``random.SystemRandom`` and are never printed or
stored, so the result cannot be mapped back.  Afterwards every original
identifier and every changed number is searched for in the output, both in
the extracted text and in the raw decompressed PDF objects.

Usage (pikepdf is only needed for this script)::

    uv run --with pikepdf python scripts/anonymize_morganstanley_statements.py \\
        OUT_DIR statement_q1.pdf statement_q2.pdf statement_q3.pdf statement_q4.pdf
"""

import math
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pikepdf

FAKE_ADDRESS = ["Erika Mustermann", "MUSTERSTRASSE 1", "ZURICH  8000", "SWITZERLAND"]
FAKE_ACCOUNT = "MS12345678"
PUBLIC_WORDS = {"ZURICH", "SWITZERLAND"}  # kept on purpose; not identifying on their own

RNG = random.SystemRandom()
DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2}$")
NUM_RE = re.compile(r"^\$?\(?\$?\d[\d,]*\.\d+\)?$")
GRANT_RE = re.compile(r"^C\d{5,8}$")


# ---------------------------------------------------------------------------
# Low-level: text runs in the content stream
# ---------------------------------------------------------------------------


@dataclass
class Run:
    page: int
    tf_idx: int
    tm_idx: int
    tj_idx: int
    font: str
    size: float
    tm: List[float]
    text: str
    new_text: Optional[str] = None
    align_right: bool = True

    @property
    def x(self) -> float:
        return self.tm[4]

    @property
    def y(self) -> float:
        return self.tm[5]

    @property
    def rotated(self) -> bool:
        return abs(self.tm[1]) > 0.5


@dataclass
class Doc:
    path: Path
    pdf: pikepdf.Pdf
    ops: List[List[Tuple[list, pikepdf.Operator]]]
    runs: List[Run]
    widths: Dict[Tuple[int, str], Tuple[int, List[float]]]
    extra_ops: Dict[int, Dict[int, list]] = field(default_factory=dict)


def load_doc(path: Path) -> Doc:
    pdf = pikepdf.open(path)
    all_ops, runs, widths = [], [], {}
    for p, page in enumerate(pdf.pages):
        for name, font in page.obj.Resources.Font.items():
            widths[(p, str(name))] = (int(font.FirstChar), [float(w) for w in font.Widths])
        ops = [(list(operands), op) for operands, op in pikepdf.parse_content_stream(page)]
        all_ops.append(ops)
        font = size = None
        tf_idx = tm_idx = -1
        tm: List[float] = []
        for i, (operands, op) in enumerate(ops):
            name = str(op)
            if name == "Tf":
                font, size, tf_idx = str(operands[0]), float(operands[1]), i
            elif name == "Tm":
                tm, tm_idx = [float(v) for v in operands], i
            elif name == "Tj":
                runs.append(
                    Run(p, tf_idx, tm_idx, i, font, size, tm, bytes(operands[0]).decode("latin-1"))
                )
            elif name in ("TJ", "'", '"'):
                raise NotImplementedError(f"{path}: unexpected text operator {name}")
    return Doc(path, pdf, all_ops, runs, widths)


def text_width(doc: Doc, run: Run, text: str) -> float:
    first, widths = doc.widths[(run.page, run.font)]
    return sum(widths[ord(c) - first] for c in text) * run.size / 1000


def rows(doc: Doc, page: int) -> Dict[float, List[Run]]:
    out: Dict[float, List[Run]] = {}
    for r in doc.runs:
        if r.page == page and not r.rotated:
            out.setdefault(round(r.y, 2), []).append(r)
    return {y: sorted(rs, key=lambda r: r.x) for y, rs in out.items()}


# ---------------------------------------------------------------------------
# Number formatting that mimics the original token
# ---------------------------------------------------------------------------


def parse_num(token: str) -> Decimal:
    neg = "(" in token
    value = Decimal(token.replace("$", "").replace(",", "").replace("(", "").replace(")", ""))
    return -value if neg else value


def format_like(token: str, value: Decimal) -> str:
    decimals = len(token.split(".")[1].rstrip(")"))
    q = Decimal(1).scaleb(-decimals)
    body = f"{abs(value.quantize(q, rounding=ROUND_HALF_UP)):,.{decimals}f}"
    if "," not in token.replace("$", "") and abs(parse_num(token)) >= 1000:
        body = body.replace(",", "")
    neg = value < 0 or "(" in token
    dollar_outside = token.startswith("$(")
    if neg:
        return (
            f"$({body})" if dollar_outside else ("$" if token.startswith("$") else "") + f"({body})"
        )
    return ("$" if token.startswith("$") else "") + body


def money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def shares(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def log_uniform(lo: float, hi: float) -> float:
    return math.exp(RNG.uniform(math.log(lo), math.log(hi)))


# ---------------------------------------------------------------------------
# Semantic model of one statement
# ---------------------------------------------------------------------------


@dataclass
class ShareRow:
    doc: Doc
    date: date
    activity: str
    nums: List[Run]


@dataclass
class AwardRow:
    doc: Doc
    date: date
    grant: Run
    shares: Run
    net_shares: Run
    price: Run
    gross: Run
    taxes: Run
    net: Run


def to_date(text: str) -> date:
    m, d, y = (int(v) for v in text.split("/"))
    return date(2000 + y, m, d)


def summary_values(doc: Doc, label: str) -> List[Run]:
    """Opening and closing value next to *label* in the page-1 summary box."""
    page1 = [r for r in doc.runs if r.page == 0 and not r.rotated]
    anchor = next((r for r in page1 if r.text == label), None)
    if anchor is None:
        raise ValueError(f"{doc.path}: summary row {label!r} not found")
    values = sorted(
        (r for r in page1 if abs(r.y - anchor.y) < 2 and r.x > anchor.x and NUM_RE.match(r.text)),
        key=lambda r: r.x,
    )
    if len(values) != 2:
        raise ValueError(
            f"{doc.path}: expected 2 values for {label!r}, got {[r.text for r in values]}"
        )
    return values


def share_rows(doc: Doc) -> List[ShareRow]:
    out = []
    for p in range(len(doc.ops)):
        for _, rs in sorted(rows(doc, p).items(), key=lambda kv: -kv[0]):
            if (
                len(rs) >= 2
                and DATE_RE.match(rs[0].text)
                and abs(rs[0].size - 8.0) < 0.01
                and abs(rs[0].x - 36.0) < 1
            ):
                activity = rs[1].text.strip()
                nums = [r for r in rs[2:] if NUM_RE.match(r.text.strip())]
                out.append(ShareRow(doc, to_date(rs[0].text), activity, nums))
    return out


def award_rows(doc: Doc) -> List[AwardRow]:
    out = []
    for p in range(len(doc.ops)):
        page_rows = rows(doc, p)
        for y, rs in sorted(page_rows.items(), key=lambda kv: -kv[0]):
            texts = [r.text for r in rs]
            if (
                len(rs) >= 6
                and DATE_RE.match(texts[0])
                and "Release" in texts
                and GRANT_RE.match(texts[2])
            ):
                second = next(v for k, v in page_rows.items() if abs(k - (y - 8.4)) < 0.3)
                nums1 = [r for r in rs if NUM_RE.match(r.text)]
                nums2 = [r for r in second if NUM_RE.match(r.text)]
                price, net_shares = nums2[0], nums2[1]
                out.append(
                    AwardRow(
                        doc, to_date(texts[0]), rs[2], nums1[0], net_shares, price, *nums1[1:4]
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Rewriting quantities and amounts
# ---------------------------------------------------------------------------


def cluster_factors(quantities: List[Decimal]) -> Dict[Decimal, float]:
    """Group similar lot sizes and give each group its own random target size."""
    factors: Dict[Decimal, float] = {}
    clusters: List[List[Decimal]] = []
    for q in sorted(set(quantities)):
        if clusters and q <= clusters[-1][0] * Decimal("1.08"):
            clusters[-1].append(q)
        else:
            clusters.append([q])
    for c in clusters:
        mean = float(sum(c) / len(c))
        target = log_uniform(4, 60)
        for q in c:
            factors[q] = target / mean
    return factors


def jitter() -> Decimal:
    return Decimal(str(RNG.uniform(0.97, 1.03)))


def set_num(run: Run, value: Decimal) -> None:
    run.new_text = format_like(run.text.strip(), value)


def anonymize(docs: List[Doc]) -> None:
    docs = sorted(docs, key=lambda d: to_date(re.search(r"\(as of (\S+)\)", " ".join(r.text for r in d.runs)).group(1)))  # type: ignore[union-attr]
    srows = [r for d in docs for r in share_rows(d)]
    arows = [r for d in docs for r in award_rows(d)]

    # --- new share quantities for deposited releases (lot-size clusters)
    release_lots = [r for r in srows if r.activity == "Release" and len(r.nums) == 2]
    factors = cluster_factors([parse_num(r.nums[0].text) for r in release_lots])
    new_lot: Dict[int, Decimal] = {}
    for r in release_lots:
        q = parse_num(r.nums[0].text)
        new_lot[id(r)] = max(shares(q * Decimal(str(factors[q])) * jitter()), Decimal("0.500"))
        set_num(r.nums[0], new_lot[id(r)])

    # --- sell-at-vest releases: per-grant scale, new tax rate per vest date
    grant_ids = {a.grant.text for a in arows}
    fake_grants = {g: f"C{1000001 + i}" for i, g in enumerate(sorted(grant_ids))}
    grant_target = {g: log_uniform(4, 60) for g in grant_ids}
    grant_mean = {
        g: float(sum(parse_num(a.shares.text) for a in arows if a.grant.text == g))
        / sum(1 for a in arows if a.grant.text == g)
        for g in grant_ids
    }
    tax_rate = {d: Decimal(str(round(RNG.uniform(0.28, 0.42), 4))) for d in {a.date for a in arows}}
    award_net: Dict[Tuple[int, Decimal], Decimal] = {}
    for a in arows:
        g = a.grant.text
        q = shares(
            parse_num(a.shares.text) * Decimal(str(grant_target[g] / grant_mean[g])) * jitter()
        )
        gross = money(q * parse_num(a.price.text))
        taxes = money(gross * tax_rate[a.date])
        award_net[(id(a.doc), parse_num(a.net.text))] = gross - taxes
        set_num(a.shares, q)
        set_num(a.net_shares, q)
        set_num(a.gross, gross)
        set_num(a.taxes, taxes)
        set_num(a.net, gross - taxes)
        a.grant.new_text = fake_grants[g]
        a.grant.align_right = False

    # --- walk the year in statement order, recomputing holdings and cash
    open_shares_run = summary_values(docs[0], "Number of Shares")[0]
    open_cash_run = summary_values(docs[0], "Cash Value")[0]
    orig_holdings = parse_num(open_shares_run.text)
    orig_cash = parse_num(open_cash_run.text)
    holdings = shares(Decimal(str(log_uniform(20, 90))))
    cash = money(Decimal(str(RNG.uniform(40, 400))))
    used_lots: set = set()
    pending_wht: Optional[Tuple[Decimal, Decimal]] = None  # (new dividend, new withholding)

    for d in docs:
        sh_open, sh_close = summary_values(d, "Number of Shares")
        cash_open, cash_close = summary_values(d, "Cash Value")
        set_num(sh_open, holdings)
        set_num(cash_open, cash)
        for r in [r for r in srows if r.doc is d]:
            n = [parse_num(x.text) for x in r.nums]
            if r.activity == "Release" and len(n) == 2:
                orig_holdings += n[0]
                holdings += new_lot[id(r)]
            elif r.activity == "Release" and len(n) == 1:
                new = award_net[(id(d), n[0])]
                orig_cash += n[0]
                cash += new
                set_num(r.nums[0], new)
            elif r.activity == "Sale":
                orig_q = -n[0]
                if orig_q == orig_holdings:
                    new_q = holdings
                else:
                    match = next(
                        (
                            x
                            for x in release_lots
                            if id(x) not in used_lots and parse_num(x.nums[0].text) == orig_q
                        ),
                        None,
                    )
                    if match is not None:
                        used_lots.add(id(match))
                        new_q = new_lot[id(match)]
                    else:
                        new_q = shares(orig_q * holdings / orig_holdings)
                gross = money(new_q * n[1])
                net = gross - money((n[2] - n[3]) * gross / n[2]) if n[2] else gross
                orig_holdings -= orig_q
                orig_cash += n[3]
                holdings -= new_q
                cash += net
                set_num(r.nums[0], -new_q)
                set_num(r.nums[2], gross)
                set_num(r.nums[3], net)
            elif r.activity == "Dividend Credit":
                per_share = (n[0] / orig_holdings).quantize(Decimal("0.0001"))
                div = money(holdings * per_share)
                orig_cash += n[0]
                cash += div
                for x in r.nums:
                    set_num(x, div)
                pending_wht = (div, Decimal(0))
                last_div = (n[0], div)
            elif r.activity == "Withholding Tax":
                rate = (-n[0] / last_div[0]).quantize(Decimal("0.01"))
                wht = money(last_div[1] * rate)
                orig_cash += n[0]
                cash -= wht
                set_num(r.nums[0], -wht)
                pending_wht = (last_div[1], wht)
                last_wht = (-n[0], last_div[0])
            elif r.activity == "Cancel Withholding Tax":
                net_rate = ((last_wht[0] - n[0]) / last_wht[1]).quantize(Decimal("0.01"))
                assert pending_wht is not None
                refund = pending_wht[1] - money(pending_wht[0] * net_rate)
                orig_cash += n[0]
                cash += refund
                set_num(r.nums[0], refund)
            elif r.activity == "Proceeds Disbursement":
                amount = -n[0] if n[0] < 0 else n[0]
                if amount != orig_cash:
                    raise ValueError(
                        f"{d.path}: disbursement {amount} is not the full cash balance"
                    )
                orig_cash -= amount
                set_num(r.nums[0], -cash)
                cash = Decimal("0.00")
            else:
                raise NotImplementedError(f"{d.path}: activity {r.activity!r}")
        set_num(sh_close, holdings)
        set_num(cash_close, cash)
        prices = summary_values(d, "Share Price")
        values = summary_values(d, "Share Value")
        totals = summary_values(d, "Total Account Value")
        unsettled = summary_values(d, "Net Unsettled Cash")
        for i, (h, c) in enumerate(
            (
                (parse_num(sh_open.new_text or ""), parse_num(cash_open.new_text or "")),
                (holdings, cash),
            )
        ):
            value = money(h * parse_num(prices[i].text))
            set_num(values[i], value)
            set_num(totals[i], value + c + parse_num(unsettled[i].text))
        if orig_holdings != parse_num(sh_close.text) or orig_cash != parse_num(cash_close.text):
            raise ValueError(f"{d.path}: original statement does not reconcile; model incomplete")

    # --- identity
    for d in docs:
        for r in d.runs:
            if r.text == d_account(docs):
                r.new_text, r.align_right = FAKE_ACCOUNT, False
        replace_address_block(d)


def d_account(docs: List[Doc]) -> str:
    runs = docs[0].runs
    idx = next(i for i, r in enumerate(runs) if r.text == "Account Number:")
    return runs[idx + 1].text


def address_lines(doc: Doc) -> List[List[Run]]:
    lines: Dict[float, List[Run]] = {}
    for r in doc.runs:
        if r.page == 0 and r.rotated:
            lines.setdefault(round(r.x, 2), []).append(r)
    return [sorted(v, key=lambda r: r.y) for _, v in sorted(lines.items())]


def replace_address_block(doc: Doc) -> None:
    lines = address_lines(doc)
    code, *addr = lines
    code_text = "".join(r.text for r in code)
    fake_code = re.sub(r"\d(?=\d*\*$)", "0", code_text)
    replace_char_line(doc, code, fake_code)
    if len(addr) != len(FAKE_ADDRESS):
        raise ValueError(f"{doc.path}: expected {len(FAKE_ADDRESS)} address lines, got {len(addr)}")
    for line, text in zip(addr, FAKE_ADDRESS):
        replace_char_line(doc, line, text)


def replace_char_line(doc: Doc, line: List[Run], text: str) -> None:
    """Re-typeset a rotated one-glyph-per-run line with new text."""
    y = line[0].y
    for i, ch in enumerate(text):
        if i < len(line):
            run = line[i]
        else:  # clone the last glyph's operators
            last = line[-1]
            run = Run(
                last.page,
                last.tf_idx,
                last.tm_idx,
                last.tj_idx,
                last.font,
                last.size,
                list(last.tm),
                last.text,
            )
            doc.extra_ops.setdefault(last.page, {}).setdefault(last.tj_idx, []).append(run)
        run.new_text, run.align_right = ch, False
        run.tm = run.tm[:5] + [y]
        y += text_width(doc, run, ch)
    for run in line[len(text) :]:
        run.new_text = ""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def dec(v: float) -> Decimal:
    return Decimal(f"{v:.4f}")


def write_doc(doc: Doc, out: Path) -> None:
    for p, page in enumerate(doc.pdf.pages):
        ops = doc.ops[p]
        for r in [r for r in doc.runs if r.page == p and r.new_text is not None]:
            tm = list(r.tm)
            if r.align_right and not r.rotated:
                tm[4] = r.x + text_width(doc, r, r.text) - text_width(doc, r, r.new_text)
            ops[r.tm_idx] = ([dec(v) for v in tm], ops[r.tm_idx][1])
            ops[r.tj_idx] = ([pikepdf.String(r.new_text.encode("latin-1"))], ops[r.tj_idx][1])
        new_ops = []
        for i, op in enumerate(ops):
            new_ops.append(op)
            for extra in doc.extra_ops.get(p, {}).get(i, []):
                new_ops.append(ops[extra.tf_idx])
                new_ops.append(([dec(v) for v in extra.tm], pikepdf.Operator("Tm")))
                new_ops.append(
                    (
                        [pikepdf.String((extra.new_text or "").encode("latin-1"))],
                        pikepdf.Operator("Tj"),
                    )
                )
        page.obj.Contents = doc.pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
    doc.pdf.docinfo["/CreationDate"] = "D:20260101000000Z"
    doc.pdf.docinfo["/ModDate"] = "D:20260101000000Z"
    doc.pdf.save(out, deterministic_id=True, compress_streams=True)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def forbidden_tokens(docs: List[Doc]) -> Tuple[List[str], List[str]]:
    """Return (identity tokens, changed numbers) of the original statements."""
    identity = {d_account(docs), d_account(docs)[2:]}
    numbers = set()
    for d in docs:
        for line in address_lines(d):
            text = "".join(r.text for r in line)
            identity.update(
                w for w in re.findall(r"[A-Za-z0-9]{3,}", text) if w not in PUBLIC_WORDS
            )
            identity.update(re.findall(r"\d{4,}", text))
        for r in d.runs:
            if GRANT_RE.match(r.text):
                identity.add(r.text)
            if r.new_text is not None and NUM_RE.match(r.text.strip()):
                raw = r.text.strip().replace("$", "").replace("(", "").replace(")", "")
                if parse_num(raw) != 0:
                    numbers.add(raw)
    return sorted(identity), sorted(numbers)


def all_bytes(pdf: pikepdf.Pdf) -> bytes:
    blobs = [repr(pdf.trailer).encode("latin-1", "replace")]
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            blobs.append(obj.read_bytes())
        blobs.append(repr(obj).encode("latin-1", "replace"))
    return b"\n".join(blobs)


def shown_strings(pdf: pikepdf.Pdf) -> List[str]:
    out = []
    for page in pdf.pages:
        for operands, op in pikepdf.parse_content_stream(page):
            if str(op) == "Tj":
                out.append(bytes(operands[0]).decode("latin-1"))
    return out


def verify(originals: List[Doc], outputs: List[Path]) -> None:
    identity, numbers = forbidden_tokens(originals)
    leaks = []
    for out in outputs:
        pdf = pikepdf.open(out)
        raw = all_bytes(pdf)
        strings = shown_strings(pdf)
        joined = "\n".join(strings)
        for t in identity:
            if t.encode() in raw or t.lower() in joined.lower():
                leaks.append(f"{out.name}: identity token {t!r}")
        for t in numbers:
            # Whole-number match: "12.34" must not flag a new value like "12.345".
            pattern = re.compile(rf"(?<![\d.,]){re.escape(t)}(?![\d])")
            if any(pattern.search(s) for s in strings):
                leaks.append(f"{out.name}: original number {t!r}")
    if leaks:
        raise SystemExit("LEAK CHECK FAILED:\n  " + "\n  ".join(leaks))
    if not identity or not numbers:
        raise SystemExit("LEAK CHECK FAILED: nothing to check, the token collection is broken")
    print(
        f"Leak check passed: {len(identity)} identity tokens absent from all PDF objects and "
        f"{len(numbers)} changed original numbers absent from all page text in {len(outputs)} files."
    )


def main() -> None:
    out_dir = Path(sys.argv[1])
    paths = [Path(p) for p in sys.argv[2:]]
    docs = [load_doc(p) for p in paths]
    anonymize(docs)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for d in docs:
        closing = re.findall(r"\(as of (\d+)/(\d+)/(\d+)\)", " ".join(r.text for r in d.runs))[1]
        out = (
            out_dir
            / f"quarterly_statement_20{closing[2]}-{int(closing[0]):02d}-{int(closing[1]):02d}.pdf"
        )
        write_doc(d, out)
        outputs.append(out)
        print(f"wrote {out}")
    # Runs keep their original text next to new_text, so the anonymised
    # documents still know every original value to search for.
    verify(docs, outputs)


if __name__ == "__main__":
    main()
