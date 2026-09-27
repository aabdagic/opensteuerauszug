"""Morgan Stanley at Work (Shareworks) stock plan importer.

Builds a TaxStatement from the quarterly statement PDFs of a Morgan
Stanley Smith Barney stock plan account (e.g. Alphabet GSUs).

Key design choices
------------------
* Input is a directory; every PDF in it is inspected and anything that is
  not a quarterly statement (Account Summary, 1042-S, ...) is skipped.
* The quarterly statements must cover the whole tax period without gaps,
  and each statement's closing balances must equal the next statement's
  opening balances.  Share and cash movements are additionally reconciled
  against the quarter-end balances by the shared ``PositionReconciler``.
  All statements must belong to one account and one issuer; two different
  copies of the same quarter are rejected.
* ``Release`` rows with a quantity and price are vested shares deposited
  into the account (mutation at the vest price).  ``Release`` rows with a
  single amount are sell-at-vest (trading plan) net proceeds; each is matched
  to its entry in the award section and recorded as the shares received and
  sold on the vest date, plus the net cash.
* Each ``Cancel Withholding Tax`` refund is netted into the withholding of the
  dividend it belongs to: the one it brings down to the US treaty rate of 15%
  (matching how the 1042-S reports the year).  Statements of the previous or
  following year in the input directory are used for refunds that cross the
  year boundary; a refund that matches nothing stops the import.
* Statements carry no ISIN or ticker, only an issuer description.  Known
  issuers are mapped below; others need ``symbol``/``isin`` in the account
  configuration.
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from opensteuerauszug.config.models import MorganStanleyAccountSettings
from opensteuerauszug.core.position_reconciler import PositionReconciler
from opensteuerauszug.importers.common import (
    CashAccountEntry,
    PositionHints,
    SecurityNameRegistry,
    SecurityPositionData,
    augment_list_of_bank_accounts,
    augment_list_of_securities,
    build_client,
    build_security_payment,
    parse_swiss_canton,
    resolve_first_last_name,
)
from opensteuerauszug.model.ech0196 import (
    Institution,
    ISINType,
    SecurityPayment,
    SecurityStock,
    TaxStatement,
)
from opensteuerauszug.model.position import SecurityPosition

from .statement_parser import (
    ActivityKind,
    AwardEntry,
    QuarterlyStatement,
    StatementLine,
    load_statement_pdf,
)

logger = logging.getLogger(__name__)

CURRENCY = "USD"

# Residual US withholding on dividends for Swiss residents under the treaty.
US_TREATY_RATE = Decimal("0.15")
# Refund matching tolerance: the statement rounds every amount to cents.
CENT_TOLERANCE = Decimal("0.011")

# Issuer description (as printed on the statement) -> (symbol, ISIN)
_KNOWN_ISSUERS: Dict[str, Tuple[str, str]] = {
    "ALPHABET INC CL C": ("GOOG", "US02079K1079"),
    "ALPHABET INC CL A": ("GOOGL", "US02079K3059"),
}


@dataclass
class _Withholding:
    """A withholding row and the dividend it was taken from."""

    paid: date
    amount: Decimal  # negative, as printed
    dividend: Decimal
    source: str
    refunded: Decimal = Decimal("0")

    @property
    def net_withheld(self) -> Decimal:
        return -self.amount - self.refunded


class MorganStanleyImporter:
    """Import Morgan Stanley at Work quarterly statements for a tax period."""

    def __init__(
        self,
        period_from: date,
        period_to: date,
        account_settings_list: List[MorganStanleyAccountSettings],
        strict_consistency: bool = True,
    ) -> None:
        self.period_from = period_from
        self.period_to = period_to
        self.account_settings_list = account_settings_list
        self.strict_consistency = strict_consistency

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def import_dir(self, directory: str) -> TaxStatement:
        statements: List[QuarterlyStatement] = []
        for fname in sorted(os.listdir(directory)):
            if not fname.lower().endswith(".pdf"):
                continue
            statement = load_statement_pdf(os.path.join(directory, fname))
            if statement is not None:
                statements.append(statement)
        if not statements:
            raise FileNotFoundError(
                f"No Morgan Stanley quarterly statement PDFs found in {directory}"
            )
        return self.import_statements(statements)

    def import_statements(self, statements: Sequence[QuarterlyStatement]) -> TaxStatement:
        unique = _drop_identical_duplicates(statements)
        _require_single_account_and_issuer(unique)
        chain = self._select_chain(unique)

        account_number = chain[0].account_number
        settings = self._settings_for(account_number)
        symbol, isin = self._resolve_security(chain[0].issuer_description, settings)
        depot = account_number

        sec_pos = SecurityPosition(
            depot=depot,
            symbol=symbol,
            isin=ISINType(isin) if isin else None,
            description=chain[0].issuer_description,
        )
        name_registry = SecurityNameRegistry()
        name_registry.update(sec_pos, chain[0].issuer_description, 10)

        security_stocks: List[SecurityStock] = []
        cash_stocks: List[SecurityStock] = []

        for st in chain:
            self._add_balances(st, security_stocks, cash_stocks)
            sold_at_vest = _match_sold_at_vest(st)
            for line in st.lines:
                if not self.period_from <= line.trade_date <= self.period_to:
                    continue
                award = sold_at_vest.get(id(line))
                self._process_line(line, st, award, security_stocks, cash_stocks)

        payments = self._dividend_payments(unique)
        security_stocks = _dedupe_balances(security_stocks)
        cash_stocks = _dedupe_balances(cash_stocks)
        closing_cash = self._reconcile_cash(depot, cash_stocks)

        statement = TaxStatement(
            minorVersion=1,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
            listOfSecurities=None,
            listOfBankAccounts=None,
        )
        statement.institution = Institution(name="Morgan Stanley Smith Barney LLC")

        first_name, last_name = resolve_first_last_name(
            full_name=(settings.full_name if settings else None) or chain[0].holder_name
        )
        client = build_client(
            client_number=account_number, first_name=first_name, last_name=last_name
        )
        if client is not None:
            statement.client = [client]
        canton = parse_swiss_canton(settings.canton if settings else None)
        if canton:
            statement.canton = canton

        positions: Dict[SecurityPosition, SecurityPositionData] = {
            sec_pos: SecurityPositionData(stocks=security_stocks, payments=payments)
        }
        augment_list_of_securities(
            statement,
            positions,
            name_registry=name_registry,
            hints_for=lambda _sp: PositionHints(security_category="SHARE", country="US"),
            strict_consistency=self.strict_consistency,
            run_initial_consistency_check=True,
            # Each release is its own event (grant tranche); keep them apart.
            aggregate_same_day_mutations=False,
        )
        augment_list_of_bank_accounts(
            statement,
            [
                CashAccountEntry(
                    account_id=account_number,
                    currency=CURRENCY,
                    closing_balance=closing_cash,
                    payments=[],
                    country="US",
                    name=f"{account_number} {CURRENCY}",
                    number=f"{account_number}-{CURRENCY}",
                )
            ],
        )
        return statement

    # ------------------------------------------------------------------
    # Statement selection and configuration
    # ------------------------------------------------------------------

    def _select_chain(self, statements: Sequence[QuarterlyStatement]) -> List[QuarterlyStatement]:
        """The gap-free statements covering the tax period (others are only used for refunds)."""
        chain = sorted(
            (
                st
                for st in statements
                if st.closing_date >= self.period_from and st.opening_date <= self.period_to
            ),
            key=lambda st: st.opening_date,
        )
        for st in statements:
            if st not in chain:
                logger.info(
                    "%s is outside the tax period; only its withholding refunds are used", st.source
                )
        if not chain:
            raise ValueError(
                f"No quarterly statements overlap the tax period {self.period_from} "
                f"to {self.period_to}."
            )
        if chain[0].opening_date > self.period_from:
            raise ValueError(
                f"Quarterly statements start on {chain[0].opening_date}, after the tax "
                f"period start {self.period_from}. Download the missing statement(s)."
            )
        if chain[-1].closing_date < self.period_to:
            raise ValueError(
                f"Quarterly statements end on {chain[-1].closing_date}, before the tax "
                f"period end {self.period_to}. Download the missing statement(s)."
            )
        for prev, nxt in zip(chain, chain[1:]):
            if nxt.opening_date != prev.closing_date + timedelta(days=1):
                raise ValueError(
                    f"Gap between statements: {prev.source} ends {prev.closing_date}, "
                    f"{nxt.source} starts {nxt.opening_date}. Download the missing statement."
                )
            if (prev.closing_shares, prev.closing_cash) != (nxt.opening_shares, nxt.opening_cash):
                raise ValueError(
                    f"Closing balances of {prev.source} (shares {prev.closing_shares}, "
                    f"cash {prev.closing_cash}) do not match opening balances of "
                    f"{nxt.source} (shares {nxt.opening_shares}, cash {nxt.opening_cash})."
                )
        return chain

    def _settings_for(self, account_number: str) -> Optional[MorganStanleyAccountSettings]:
        normalized = _normalize_account(account_number)
        for settings in self.account_settings_list:
            if _normalize_account(settings.account_number) == normalized:
                return settings
        if self.account_settings_list:
            # Never borrow another account's settings: its symbol/ISIN would
            # silently mislabel this account's security.
            logger.warning(
                "Morgan Stanley account %s is not configured (configured: %s); "
                "using general settings only.",
                account_number,
                ", ".join(s.account_number for s in self.account_settings_list),
            )
        return None

    @staticmethod
    def _resolve_security(
        issuer_description: str, settings: Optional[MorganStanleyAccountSettings]
    ) -> Tuple[str, Optional[str]]:
        known = _KNOWN_ISSUERS.get(issuer_description.upper())
        symbol = (settings.symbol if settings else None) or (known[0] if known else None)
        isin = (settings.isin if settings else None) or (known[1] if known else None)
        if not symbol:
            raise ValueError(
                f"Unknown issuer {issuer_description!r}: set 'symbol' (and ideally 'isin') "
                "for this account in config.toml under [brokers.morganstanley.accounts.<alias>]."
            )
        return symbol, isin

    # ------------------------------------------------------------------
    # Row handling
    # ------------------------------------------------------------------

    def _add_balances(
        self,
        st: QuarterlyStatement,
        security_stocks: List[SecurityStock],
        cash_stocks: List[SecurityStock],
    ) -> None:
        closing_ref = st.closing_date + timedelta(days=1)
        for ref, shares, cash, price in (
            (st.opening_date, st.opening_shares, st.opening_cash, st.opening_share_price),
            (closing_ref, st.closing_shares, st.closing_cash, st.closing_share_price),
        ):
            if not self.period_from <= ref <= self.period_to + timedelta(days=1):
                continue
            security_stocks.append(
                SecurityStock(
                    referenceDate=ref,
                    mutation=False,
                    quantity=shares,
                    unitPrice=price,
                    balanceCurrency=CURRENCY,
                    quotationType="PIECE",
                )
            )
            cash_stocks.append(
                SecurityStock(
                    referenceDate=ref,
                    mutation=False,
                    quantity=cash,
                    balanceCurrency=CURRENCY,
                    quotationType="PIECE",
                )
            )

    def _process_line(
        self,
        line: StatementLine,
        st: QuarterlyStatement,
        award: Optional[AwardEntry],
        security_stocks: List[SecurityStock],
        cash_stocks: List[SecurityStock],
    ) -> None:
        n = line.numbers
        context = f"{st.source}: {line.text!r}"

        if line.kind == ActivityKind.RELEASE:
            if len(n) == 2:
                security_stocks.append(_mutation(line.trade_date, n[0], n[1], "Release (vesting)"))
            elif len(n) == 1 and award is not None:
                # Sold at vest: the shares are received and sold on the vest date.
                security_stocks.append(
                    _mutation(line.trade_date, award.shares, award.price, "Release (sold at vest)")
                )
                security_stocks.append(
                    _mutation(line.trade_date, -award.shares, award.price, "Sale at vest")
                )
                cash_stocks.append(_cash(line.trade_date, n[0], "Release proceeds (net)"))
            else:
                raise ValueError(f"Unexpected Release row format in {context}")

        elif line.kind == ActivityKind.SALE:
            if len(n) < 3 or n[0] >= 0:
                raise ValueError(f"Unexpected Sale row format in {context}")
            security_stocks.append(_mutation(line.trade_date, n[0], n[1], "Sale"))
            cash_stocks.append(_cash(line.trade_date, n[-1], "Sale proceeds"))

        elif line.kind == ActivityKind.DIVIDEND:
            _expect_numbers(n, context)
            cash_stocks.append(_cash(line.trade_date, n[0], "Dividend"))

        elif line.kind == ActivityKind.WITHHOLDING:
            _expect_numbers(n, context)
            cash_stocks.append(_cash(line.trade_date, n[0], "Withholding tax"))

        elif line.kind == ActivityKind.WITHHOLDING_CANCEL:
            _expect_numbers(n, context)
            cash_stocks.append(_cash(line.trade_date, abs(n[0]), "Withholding tax refund"))

        elif line.kind == ActivityKind.DISBURSEMENT:
            _expect_numbers(n, context)
            cash_stocks.append(_cash(line.trade_date, -abs(n[0]), "Proceeds disbursement"))

        else:
            raise NotImplementedError(
                f"Unknown Morgan Stanley activity {line.activity!r} in {context}. "
                "The row may be tax-relevant; please report it so support can be added."
            )

    # ------------------------------------------------------------------
    # Dividends and withholding
    # ------------------------------------------------------------------

    def _dividend_payments(self, statements: Sequence[QuarterlyStatement]) -> List[SecurityPayment]:
        """Dividend and (refund-netted) withholding payments of the tax period.

        Every statement is read, including ones outside the tax period, so that
        refunds booked in the following year reach last year's withholding and
        refunds of last year's withholding are not mistaken for this year's.
        """
        dividends: Dict[date, Decimal] = {}
        withholdings: List[_Withholding] = []
        for st in sorted(statements, key=lambda s: s.opening_date):
            for line in st.lines:
                n = line.numbers
                if line.kind == ActivityKind.DIVIDEND:
                    dividends[line.trade_date] = dividends.get(line.trade_date, Decimal(0)) + n[0]
                elif line.kind == ActivityKind.WITHHOLDING:
                    if line.trade_date not in dividends:
                        raise NotImplementedError(
                            f"{st.source}: withholding tax on {line.trade_date} without a "
                            "dividend on the same date is not supported yet. Please report it."
                        )
                    withholdings.append(
                        _Withholding(line.trade_date, n[0], dividends[line.trade_date], st.source)
                    )
                elif line.kind == ActivityKind.WITHHOLDING_CANCEL:
                    self._apply_refund(line, abs(n[0]), withholdings, st.source)

        in_period = lambda d: self.period_from <= d <= self.period_to  # noqa: E731
        payments: List[SecurityPayment] = []
        for paid, amount in sorted(dividends.items()):
            if in_period(paid):
                payments.append(
                    build_security_payment(
                        payment_date=paid,
                        description="Dividend",
                        currency=CURRENCY,
                        amount=amount,
                        broker_label="Dividend Credit",
                    )
                )
        for w in withholdings:
            if in_period(w.paid):
                payments.append(_withholding(w.paid, -w.net_withheld))
        return payments

    def _apply_refund(
        self,
        line: StatementLine,
        refund: Decimal,
        withholdings: List[_Withholding],
        source: str,
    ) -> None:
        """Net *refund* into the earliest withholding it brings down to the treaty rate."""
        for w in withholdings:
            if w.paid > line.trade_date:
                continue
            remaining = w.net_withheld - refund
            if remaining >= 0 and abs(remaining - w.dividend * US_TREATY_RATE) <= CENT_TOLERANCE:
                w.refunded += refund
                logger.info(
                    "%s: withholding refund of %s on %s netted into the withholding of %s "
                    "(dividend %s, now %s withheld)",
                    source,
                    refund,
                    line.trade_date,
                    w.paid,
                    w.dividend,
                    w.net_withheld,
                )
                return
        raise ValueError(
            f"{source}: withholding refund of {refund} on {line.trade_date} does not bring any "
            f"earlier withholding down to the {US_TREATY_RATE:.0%} treaty rate. If it belongs to "
            "a dividend of the previous year, add that year's last quarterly statement to the "
            "input directory; otherwise please report it."
        )

    def _reconcile_cash(self, depot: str, cash_stocks: List[SecurityStock]) -> Decimal:
        identifier = f"Cash-{depot}-{CURRENCY}"
        checker = PositionReconciler(list(cash_stocks), identifier=identifier)
        is_consistent, _ = checker.check_consistency(
            print_log=True,
            raise_on_error=self.strict_consistency,
            assume_zero_if_no_balances=False,
        )
        if not is_consistent:
            logger.warning(
                "%s: cash movements do not reconcile with statement balances", identifier
            )
        end_pos = PositionReconciler(
            list(cash_stocks), identifier=identifier
        ).synthesize_position_at_date(self.period_to + timedelta(days=1))
        return end_pos.quantity if end_pos else Decimal("0")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _drop_identical_duplicates(
    statements: Sequence[QuarterlyStatement],
) -> List[QuarterlyStatement]:
    """Keep one copy of repeated downloads; reject copies that differ."""
    by_period: Dict[Tuple[date, date], QuarterlyStatement] = {}
    for st in statements:
        key = (st.opening_date, st.closing_date)
        first = by_period.get(key)
        if first is None:
            by_period[key] = st
        elif first.content_key() == st.content_key():
            logger.info("Ignoring %s: identical copy of %s", st.source, first.source)
        else:
            raise ValueError(
                f"{first.source} and {st.source} cover the same period "
                f"({st.opening_date} to {st.closing_date}) but differ. Keep only the "
                "correct one (e.g. the corrected statement) in the input directory."
            )
    return list(by_period.values())


def _require_single_account_and_issuer(statements: Sequence[QuarterlyStatement]) -> None:
    accounts = {st.account_number for st in statements}
    if len(accounts) > 1:
        raise ValueError(
            "Statements for more than one Morgan Stanley account found "
            f"({', '.join(sorted(accounts))}); import each account separately."
        )
    issuers = {st.issuer_description.upper() for st in statements}
    if len(issuers) > 1:
        raise ValueError(
            f"Statements name different securities ({', '.join(sorted(issuers))}). "
            "Only one plan security per account is supported; please report this case."
        )


def _match_sold_at_vest(st: QuarterlyStatement) -> Dict[int, AwardEntry]:
    """Pair each single-amount ``Release`` row with its award-section entry.

    The pairing verifies that the amount really is the net proceeds of a
    release sold at vest.  Award entries without such a row must belong to a
    share release (a ``Release`` row with quantity and price on the same date).
    """
    unmatched = list(st.awards)
    for award in unmatched:
        if award.activity.lower() != "release":
            raise NotImplementedError(
                f"{st.source}: award activity {award.activity!r} ({award.grant_id}) is not "
                "supported yet. Please report it."
            )
        if award.gross - sum(award.amounts[1:-1], Decimal(0)) != award.net:
            raise ValueError(
                f"{st.source}: award {award.grant_id} on {award.activity_date}: gross "
                f"{award.gross} minus fees and taxes is not the net amount {award.net}."
            )

    matches: Dict[int, AwardEntry] = {}
    for line in st.lines:
        if line.kind != ActivityKind.RELEASE or len(line.numbers) != 1:
            continue
        award = next(
            (
                a
                for a in unmatched
                if a.activity_date == line.trade_date and a.net == line.numbers[0]
            ),
            None,
        )
        if award is None:
            raise ValueError(
                f"{st.source}: cannot find the award entry for {line.text!r}; "
                "the release amount cannot be verified."
            )
        if award.shares != award.net_shares:
            raise NotImplementedError(
                f"{st.source}: award {award.grant_id} on {award.activity_date} sold only part "
                "of the released shares; this case is not supported yet. Please report it."
            )
        unmatched.remove(award)
        matches[id(line)] = award

    share_release_dates = {
        ln.trade_date for ln in st.lines if ln.kind == ActivityKind.RELEASE and len(ln.numbers) == 2
    }
    for award in unmatched:
        if award.activity_date not in share_release_dates:
            raise ValueError(
                f"{st.source}: award {award.grant_id} on {award.activity_date} has no matching "
                "release in the transaction list."
            )
    return matches


def _normalize_account(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _expect_numbers(numbers: Tuple[Decimal, ...], context: str) -> None:
    if not numbers:
        raise ValueError(f"Missing amount in {context}")


def _mutation(ref: date, quantity: Decimal, price: Decimal, name: str) -> SecurityStock:
    return SecurityStock(
        referenceDate=ref,
        mutation=True,
        quantity=quantity,
        unitPrice=price,
        balanceCurrency=CURRENCY,
        quotationType="PIECE",
        name=name,
    )


def _cash(ref: date, amount: Decimal, name: str) -> SecurityStock:
    return SecurityStock(
        referenceDate=ref,
        mutation=True,
        quantity=amount,
        balanceCurrency=CURRENCY,
        quotationType="PIECE",
        name=name,
    )


def _withholding(payment_date: date, amount: Decimal) -> SecurityPayment:
    return build_security_payment(
        payment_date=payment_date,
        description="Withholding tax",
        currency=CURRENCY,
        amount=amount,
        broker_label="Withholding Tax",
        is_withholding=True,
    )


def _dedupe_balances(stocks: List[SecurityStock]) -> List[SecurityStock]:
    """Drop repeated balance rows (quarter N closing == quarter N+1 opening)."""
    seen: Dict[date, Decimal] = {}
    result: List[SecurityStock] = []
    for s in stocks:
        if not s.mutation:
            if s.referenceDate in seen:
                if seen[s.referenceDate] != s.quantity:
                    raise ValueError(
                        f"Conflicting balances on {s.referenceDate}: "
                        f"{seen[s.referenceDate]} vs {s.quantity}"
                    )
                continue
            seen[s.referenceDate] = s.quantity
        result.append(s)
    return result
