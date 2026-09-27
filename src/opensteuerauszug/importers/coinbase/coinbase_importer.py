"""Coinbase importer (plain Coinbase app / coinbase.com retail accounts).

Builds a TaxStatement from the yearly "Transaction History Report".

Key design choices
------------------
* Input is a directory.  Exactly one HTML statement in it must cover the tax
  period: unfiltered ("Type: all"), starting at the first second of the period
  and with holdings "as of" its last second.  Other statements (e.g. monthly
  ones) are ignored.  Every CSV export in the directory must contain exactly
  the statement's transactions for the period.
* Crypto assets become securities of category CURRNOTE identified by their
  ticker; the calculation phase finds them in the Kursliste (CURRNOTE.TOKEN)
  and values them at the official year-end tax value.
* Year-end holdings come from the statement's Portfolio Summary (an asset that
  is not listed has a zero balance).  For every crypto asset the opening
  balance is derived backwards from the transactions and must not be negative;
  the holdings may never go negative during the year.
* ``Reward Income`` is taxable income: the importer records it as a payment
  in the price currency (USD); the calculation phase converts it to CHF.
* Fiat wallets (EUR, CHF, ...) become bank accounts with the year-end balance
  from the Portfolio Summary.  Coinbase exports only show fiat deposits, not
  the fiat debited for purchases, so the balance can only be checked for
  plausibility (a warning, not an error).
* Unknown transaction types, or signs that contradict the type, stop the
  import.
"""

import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Sequence, Tuple

from opensteuerauszug.config.models import CoinbaseAccountSettings
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
from opensteuerauszug.model.ech0196 import Institution, SecurityStock, TaxStatement
from opensteuerauszug.model.position import SecurityPosition

from .statement_parser import (
    Statement,
    Transaction,
    is_statement_html,
    matching_keys,
    parse_statement_html,
    parse_transactions_csv,
)

logger = logging.getLogger(__name__)

FIAT_CURRENCIES = {"EUR", "USD", "CHF", "GBP"}

# Transaction type (lower case) -> required sign of the quantity (+1, -1 or 0 = either)
CRYPTO_KINDS: Dict[str, int] = {
    "buy": 1,
    "sell": -1,
    "convert": 0,
    "send": -1,
    "receive": 1,
    "reward income": 1,
    "retail mgx dex send": -1,
    "retail mgx dex trade": 0,
}
FIAT_KINDS: Dict[str, int] = {"deposit": 1, "withdrawal": -1}
INCOME_KINDS = {"reward income"}

# Fiat plausibility: warn if the balance is further off than this from the estimate.
FIAT_TOLERANCE_ABSOLUTE = Decimal("50")
FIAT_TOLERANCE_RELATIVE = Decimal("0.02")


class CoinbaseImporter:
    """Import a Coinbase transaction history report for a tax period."""

    def __init__(
        self,
        period_from: date,
        period_to: date,
        account_settings_list: List[CoinbaseAccountSettings],
        strict_consistency: bool = True,
    ) -> None:
        self.period_from = period_from
        self.period_to = period_to
        self.account_settings_list = account_settings_list
        self.strict_consistency = strict_consistency
        self._start = datetime(
            period_from.year, period_from.month, period_from.day, tzinfo=timezone.utc
        )
        self._end = datetime(
            period_to.year, period_to.month, period_to.day, 23, 59, 59, tzinfo=timezone.utc
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def import_dir(self, directory: str) -> TaxStatement:
        statements: List[Statement] = []
        csv_exports: List[Tuple[str, List[Transaction]]] = []
        for fname in sorted(os.listdir(directory)):
            path = os.path.join(directory, fname)
            lower = fname.lower()
            if lower.endswith((".htm", ".html")):
                content = open(path, "rb").read()
                if is_statement_html(content.decode("utf-8", errors="replace")):
                    statements.append(parse_statement_html(content, path))
                else:
                    logger.info("Skipping %s: not a Coinbase statement", path)
            elif lower.endswith(".csv"):
                text = open(path, encoding="utf-8-sig").read()
                csv_exports.append((path, parse_transactions_csv(text, path)))
        return self.import_statements(statements, csv_exports)

    def import_statements(
        self,
        statements: Sequence[Statement],
        csv_exports: Sequence[Tuple[str, List[Transaction]]] = (),
    ) -> TaxStatement:
        statement = self._select_statement(statements)
        transactions = sorted(statement.transactions, key=lambda t: t.timestamp)
        outside = [t for t in transactions if not self._start <= t.timestamp <= self._end]
        if outside:
            raise ValueError(
                f"{statement.source}: {len(outside)} transaction(s) outside the tax period, "
                f"e.g. {outside[0].timestamp:%Y-%m-%d}."
            )
        for source, rows in csv_exports:
            in_period = [t for t in rows if self._start <= t.timestamp <= self._end]
            if not matching_keys(in_period, transactions):
                raise ValueError(
                    f"{source} does not contain the same transactions as {statement.source} "
                    "for the tax period. Export both for the same year and unfiltered."
                )

        settings = self.account_settings_list[0] if self.account_settings_list else None
        depot = settings.account_number if settings else "COINBASE"

        crypto: Dict[str, List[Transaction]] = defaultdict(list)
        fiat: Dict[str, List[Transaction]] = defaultdict(list)
        for t in transactions:
            kind = t.kind.lower()
            is_fiat = t.asset in FIAT_CURRENCIES
            allowed = FIAT_KINDS if is_fiat else CRYPTO_KINDS
            if kind not in allowed:
                raise NotImplementedError(
                    f"{statement.source}: unsupported Coinbase transaction type {t.kind!r} for "
                    f"{t.asset} on {t.timestamp:%Y-%m-%d}. It may be tax-relevant; please report it."
                )
            sign = allowed[kind]
            if t.quantity == 0 or (sign and (t.quantity > 0) != (sign > 0)):
                raise ValueError(
                    f"{statement.source}: {t.kind} of {t.asset} on {t.timestamp:%Y-%m-%d} has an "
                    f"unexpected quantity {t.quantity}."
                )
            (fiat if is_fiat else crypto)[t.asset].append(t)

        currencies = {t.price_currency for t in transactions}
        if len(currencies) > 1:
            raise NotImplementedError(
                f"{statement.source}: prices in several currencies ({sorted(currencies)}) are not supported."
            )
        price_currency = currencies.pop() if currencies else "USD"

        positions: Dict[SecurityPosition, SecurityPositionData] = {}
        names = SecurityNameRegistry()
        for asset in sorted(
            set(crypto) | {a for a in statement.holdings if a not in FIAT_CURRENCIES}
        ):
            sec_pos = SecurityPosition(depot=depot, symbol=asset, description=asset)
            names.update(sec_pos, asset, 10)
            positions[sec_pos] = self._security_data(
                statement, asset, crypto.get(asset, []), price_currency
            )

        cash_entries = [
            self._cash_entry(statement, currency, fiat.get(currency, []), transactions, depot)
            for currency in sorted(
                set(fiat) | {a for a in statement.holdings if a in FIAT_CURRENCIES}
            )
        ]

        tax_statement = TaxStatement(
            minorVersion=1,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
            listOfSecurities=None,
            listOfBankAccounts=None,
        )
        tax_statement.institution = Institution(name="Coinbase")
        if settings:
            first, last = resolve_first_last_name(full_name=settings.full_name)
            client = build_client(
                client_number=settings.account_number, first_name=first, last_name=last
            )
            if client is not None:
                tax_statement.client = [client]
            canton = parse_swiss_canton(settings.canton)
            if canton:
                tax_statement.canton = canton

        augment_list_of_securities(
            tax_statement,
            positions,
            name_registry=names,
            hints_for=lambda _sp: PositionHints(security_category="CURRNOTE", country="XV"),
            strict_consistency=self.strict_consistency,
            run_initial_consistency_check=True,
            # One row per Coinbase transaction; keep conversions and rewards apart.
            aggregate_same_day_mutations=False,
        )
        augment_list_of_bank_accounts(tax_statement, cash_entries)
        return tax_statement

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _select_statement(self, statements: Sequence[Statement]) -> Statement:
        matching = [
            s for s in statements if s.period_start == self._start and s.holdings_as_of == self._end
        ]
        if not matching:
            found = ", ".join(
                (
                    f"{os.path.basename(s.source)} ({s.period_start:%Y-%m-%d} to "
                    f"{s.holdings_as_of:%Y-%m-%d})"
                    if s.holdings_as_of
                    else os.path.basename(s.source)
                )
                for s in statements
            )
            raise ValueError(
                f"No Coinbase statement covers the tax period {self.period_from} to "
                f"{self.period_to} (found: {found or 'none'}). Generate a custom statement for "
                "the year with all assets and all transactions, as HTML."
            )
        if len(matching) > 1:
            if any(
                sorted(t.key() for t in m.transactions)
                != sorted(t.key() for t in matching[0].transactions)
                or m.holdings != matching[0].holdings
                for m in matching[1:]
            ):
                raise ValueError(
                    "Several different statements cover the tax period: "
                    + ", ".join(m.source for m in matching)
                )
        chosen = matching[0]
        if chosen.filter.strip().lower() != "type: all":
            raise ValueError(
                f"{chosen.source} is filtered ({chosen.filter!r}). Generate the statement with "
                "all assets and all transaction types."
            )
        return chosen

    def _security_data(
        self,
        statement: Statement,
        asset: str,
        rows: List[Transaction],
        price_currency: str,
    ) -> SecurityPositionData:
        closing = statement.holdings.get(asset, Decimal(0))
        opening = closing - sum((t.quantity for t in rows), Decimal(0))
        if opening < 0:
            raise ValueError(
                f"{statement.source}: the {asset} transactions add up to more than the year-end "
                f"holding (opening balance would be {opening}). The statement is incomplete."
            )
        running = opening
        for t in rows:
            running += t.quantity
            if running < 0:
                raise ValueError(
                    f"{statement.source}: {asset} balance would be negative ({running}) after "
                    f"{t.kind} on {t.timestamp:%Y-%m-%d}."
                )

        stocks: List[SecurityStock] = [
            SecurityStock(
                referenceDate=self.period_to + timedelta(days=1),
                mutation=False,
                quantity=closing,
                balanceCurrency=price_currency,
                quotationType="PIECE",
            )
        ]
        payments = []
        for t in rows:
            stocks.append(
                SecurityStock(
                    referenceDate=t.timestamp.date(),
                    mutation=True,
                    quantity=t.quantity,
                    unitPrice=t.price,
                    balanceCurrency=price_currency,
                    quotationType="PIECE",
                    name=t.kind,
                )
            )
            if t.kind.lower() in INCOME_KINDS:
                value = t.subtotal if t.subtotal is not None else (t.quantity * (t.price or 0))
                payment = build_security_payment(
                    payment_date=t.timestamp.date(),
                    description=f"{t.kind} ({t.quantity} {asset})",
                    currency=price_currency,
                    amount=abs(value),
                    broker_label=t.kind,
                )
                payment.quantity = t.quantity
                payments.append(payment)
        return SecurityPositionData(stocks=stocks, payments=payments)

    def _cash_entry(
        self,
        statement: Statement,
        currency: str,
        rows: List[Transaction],
        all_rows: Sequence[Transaction],
        depot: str,
    ) -> CashAccountEntry:
        closing = statement.holdings.get(currency, Decimal(0))
        self._check_fiat_plausibility(statement, currency, closing, rows, all_rows)
        return CashAccountEntry(
            account_id=depot,
            currency=currency,
            closing_balance=closing,
            payments=[],
            country="US",
            name=f"Coinbase {currency} wallet",
            number=f"{depot}-{currency}",
        )

    @staticmethod
    def _check_fiat_plausibility(
        statement: Statement,
        currency: str,
        closing: Decimal,
        rows: List[Transaction],
        all_rows: Sequence[Transaction],
    ) -> None:
        """Warn if deposits minus wallet-funded purchases is far from the year-end balance.

        The purchases are only reported in the price currency (USD); they are
        converted with the average rate of the deposits.  Balances carried over
        from the previous year are not visible, so this is only a warning.
        """
        deposits = [t for t in rows if t.quantity > 0]
        withdrawals = sum((t.quantity for t in rows if t.quantity < 0), Decimal(0))
        deposited = sum((t.quantity for t in deposits), Decimal(0))
        deposited_value = sum((abs(t.total or 0) for t in deposits), Decimal(0))
        spent_value = sum(
            (
                abs(t.total or 0)
                for t in all_rows
                if f"using {currency} Wallet".lower() in t.notes.lower()
            ),
            Decimal(0),
        )
        if not deposited or not deposited_value:
            if spent_value:
                logger.warning(
                    "%s: purchases paid from the %s wallet but no %s deposits in the period; "
                    "check the year-end balance of %s %s.",
                    statement.source,
                    currency,
                    currency,
                    closing,
                    currency,
                )
            return
        estimate = deposited + withdrawals - spent_value * deposited / deposited_value
        tolerance = max(FIAT_TOLERANCE_ABSOLUTE, deposited * FIAT_TOLERANCE_RELATIVE)
        if abs(estimate - closing) > tolerance:
            logger.warning(
                "%s: the %s wallet's year-end balance (%s) differs from the estimate from "
                "deposits and purchases (%s). Check it in the Coinbase app (%s wallet history).",
                statement.source,
                currency,
                closing,
                estimate.quantize(Decimal("0.01")),
                currency,
            )
        else:
            logger.info(
                "%s: %s wallet balance %s is consistent with deposits and purchases (estimate %s).",
                statement.source,
                currency,
                closing,
                estimate.quantize(Decimal("0.01")),
            )
