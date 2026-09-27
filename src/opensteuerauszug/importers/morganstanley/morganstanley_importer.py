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
* ``Release`` rows with a quantity and price are vested shares deposited
  into the account (mutation at fair market value).  ``Release`` rows with a
  single amount are sell-at-vest (trading plan) net cash proceeds and only
  move the USD cash account.
* ``Cancel Withholding Tax`` refunds are netted against the preceding
  ``Withholding Tax`` row, matching how the 1042-S reports the year.
* Statements carry no ISIN or ticker, only an issuer description.  Known
  issuers are mapped below; others need ``symbol``/``isin`` in the account
  configuration.
"""

import logging
import os
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
    QuarterlyStatement,
    StatementLine,
    load_statement_pdf,
)

logger = logging.getLogger(__name__)

CURRENCY = "USD"

# Issuer description (as printed on the statement) -> (symbol, ISIN)
_KNOWN_ISSUERS: Dict[str, Tuple[str, str]] = {
    "ALPHABET INC CL C": ("GOOG", "US02079K1079"),
    "ALPHABET INC CL A": ("GOOGL", "US02079K3059"),
}


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
        chain = self._select_statements(statements)
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
        payments: List[SecurityPayment] = []
        cash_stocks: List[SecurityStock] = []

        for st in chain:
            self._add_balances(st, security_stocks, cash_stocks)
            for line in st.lines:
                if not self.period_from <= line.trade_date <= self.period_to:
                    continue
                self._process_line(line, st, security_stocks, payments, cash_stocks)

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

    def _select_statements(
        self, statements: Sequence[QuarterlyStatement]
    ) -> List[QuarterlyStatement]:
        accounts = {st.account_number for st in statements}
        if len(accounts) > 1:
            raise ValueError(
                "Statements for more than one Morgan Stanley account found "
                f"({', '.join(sorted(accounts))}); import each account separately."
            )

        by_start: Dict[date, QuarterlyStatement] = {}
        for st in statements:
            if st.closing_date < self.period_from or st.opening_date > self.period_to:
                logger.info("Ignoring %s: outside the tax period", st.source)
                continue
            if st.opening_date in by_start:
                logger.warning(
                    "Duplicate statement for period starting %s: using %s, ignoring %s",
                    st.opening_date,
                    by_start[st.opening_date].source,
                    st.source,
                )
                continue
            by_start[st.opening_date] = st

        chain = [by_start[d] for d in sorted(by_start)]
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
            logger.warning(
                "Account %s not found in configuration; using settings of '%s'.",
                account_number,
                self.account_settings_list[0].account_name_alias,
            )
            return self.account_settings_list[0]
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
        security_stocks: List[SecurityStock],
        payments: List[SecurityPayment],
        cash_stocks: List[SecurityStock],
    ) -> None:
        n = line.numbers
        context = f"{st.source}: {line.text!r}"

        if line.kind == ActivityKind.RELEASE:
            if len(n) == 2:
                security_stocks.append(_mutation(line.trade_date, n[0], n[1], "Release (vesting)"))
            elif len(n) == 1:
                cash_stocks.append(_cash(line.trade_date, n[0], "Release (sold at vest)"))
            else:
                raise ValueError(f"Unexpected Release row format in {context}")

        elif line.kind == ActivityKind.SALE:
            if len(n) < 3 or n[0] >= 0:
                raise ValueError(f"Unexpected Sale row format in {context}")
            security_stocks.append(_mutation(line.trade_date, n[0], n[1], "Sale"))
            cash_stocks.append(_cash(line.trade_date, n[-1], "Sale proceeds"))

        elif line.kind == ActivityKind.DIVIDEND:
            _expect_numbers(n, context)
            payments.append(
                build_security_payment(
                    payment_date=line.trade_date,
                    description="Dividend",
                    currency=CURRENCY,
                    amount=n[0],
                    broker_label=line.activity,
                )
            )
            cash_stocks.append(_cash(line.trade_date, n[0], "Dividend"))

        elif line.kind == ActivityKind.WITHHOLDING:
            _expect_numbers(n, context)
            payments.append(_withholding(line.trade_date, n[0], line.activity))
            cash_stocks.append(_cash(line.trade_date, n[0], "Withholding tax"))

        elif line.kind == ActivityKind.WITHHOLDING_CANCEL:
            _expect_numbers(n, context)
            refund = abs(n[0])
            self._net_withholding_refund(line, refund, payments)
            cash_stocks.append(_cash(line.trade_date, refund, "Withholding tax refund"))

        elif line.kind == ActivityKind.DISBURSEMENT:
            _expect_numbers(n, context)
            cash_stocks.append(_cash(line.trade_date, -abs(n[0]), "Proceeds disbursement"))

        else:
            raise NotImplementedError(
                f"Unknown Morgan Stanley activity {line.activity!r} in {context}. "
                "The row may be tax-relevant; please report it so support can be added."
            )

    @staticmethod
    def _net_withholding_refund(
        line: StatementLine, refund: Decimal, payments: List[SecurityPayment]
    ) -> None:
        for idx in range(len(payments) - 1, -1, -1):
            original = payments[idx]
            if original.broker_label_original == "Withholding Tax" and original.amount is not None:
                netted = original.amount + refund
                logger.info(
                    "Netting withholding refund of %s on %s against withholding of %s on %s",
                    refund,
                    line.trade_date,
                    original.amount,
                    original.paymentDate,
                )
                payments[idx] = _withholding(original.paymentDate, netted, "Withholding Tax")
                return
        logger.warning(
            "Withholding refund on %s has no preceding withholding in the period; "
            "recording it as a separate reversal.",
            line.trade_date,
        )
        payments.append(_withholding(line.trade_date, refund, line.activity))

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


def _withholding(payment_date: date, amount: Decimal, label: str) -> SecurityPayment:
    return build_security_payment(
        payment_date=payment_date,
        description="Withholding tax",
        currency=CURRENCY,
        amount=amount,
        broker_label=label,
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
