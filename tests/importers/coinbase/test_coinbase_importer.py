import logging
from datetime import date
from decimal import Decimal

import pytest

from opensteuerauszug.importers.coinbase.coinbase_importer import CoinbaseImporter
from opensteuerauszug.importers.coinbase.statement_parser import (
    parse_statement_html,
    parse_transactions_csv,
)

from opensteuerauszug.config.models import CoinbaseAccountSettings

from .statement_data import ACCOUNT_ID, HOLDINGS_2025, YEAR_2025, statement_html, transactions_csv


def _parse(**kwargs):
    return parse_statement_html(statement_html(**kwargs).encode(), "statement.htm")


def _import(statements, csv_exports=(), settings=()):
    importer = CoinbaseImporter(date(2025, 1, 1), date(2025, 12, 31), list(settings))
    return importer.import_statements(statements, csv_exports)


def _settings(account_number):
    return CoinbaseAccountSettings(
        account_number=account_number,
        full_name="Erika Mustermann",
        canton="ZH",
        broker_name="coinbase",
        account_name_alias="main",
    )


def test_account_id_from_the_statement_is_the_client_depot_and_account_number():
    compact = ACCOUNT_ID.replace("-", "")
    tax_statement = _import([_parse()], settings=[_settings(ACCOUNT_ID.upper())])
    assert tax_statement.client[0].clientNumber == ACCOUNT_ID
    assert tax_statement.listOfSecurities.depot[0].depotNumber == compact
    assert tax_statement.listOfBankAccounts.bankAccount[0].bankAccountNumber == compact
    assert tax_statement.canton == "ZH"


def test_unconfigured_account_id_warns_and_uses_no_account_settings(caplog):
    with caplog.at_level(logging.WARNING):
        tax_statement = _import([_parse()], settings=[_settings("COINBASE")])
    assert f"Coinbase account {ACCOUNT_ID} is not configured" in caplog.text
    assert tax_statement.listOfSecurities.depot[0].depotNumber == ACCOUNT_ID.replace("-", "")
    assert tax_statement.canton is None


def test_statements_of_two_accounts_are_rejected():
    other = _parse(account_id="11111111-2222-4333-8444-555555555555")
    with pytest.raises(ValueError, match="Several different statements"):
        _import([_parse(), other])


def _securities(tax_statement):
    return {s.symbol: s for s in tax_statement.listOfSecurities.depot[0].security}


def test_each_crypto_asset_becomes_a_currnote_security():
    securities = _securities(_import([_parse()]))
    assert sorted(securities) == ["BTC", "USDC", "USDT"]
    assert {s.securityCategory for s in securities.values()} == {"CURRNOTE"}
    assert {s.country for s in securities.values()} == {"XV"}


def test_year_end_and_derived_opening_balances():
    btc = _securities(_import([_parse()]))["BTC"]
    balances = {s.referenceDate: s.quantity for s in btc.stock if not s.mutation}
    assert balances[date(2026, 1, 1)] == Decimal("0.032")
    assert balances[date(2025, 1, 1)] == Decimal("0.010")


def test_year_end_market_price_is_kept_on_the_closing_balance():
    btc = _securities(_import([_parse()]))["BTC"]
    [closing] = [s for s in btc.stock if not s.mutation and s.referenceDate == date(2026, 1, 1)]
    assert (closing.unitPrice, closing.balance, closing.balanceCurrency) == (
        Decimal("90000.00"),
        Decimal("0.032") * Decimal("90000.00"),
        "USD",
    )


def test_conversion_with_one_side_missing_is_rejected():
    rows = [r for r in YEAR_2025 if not (r[1] == "Convert" and r[2] == "BTC")]
    holdings = dict(HOLDINGS_2025, BTC="0.029")
    with pytest.raises(ValueError, match="does not list both sides"):
        _import([_parse(rows=rows, holdings=holdings)])


def test_conversion_quantities_must_match_the_note():
    rows = [
        r[:3] + ("0.004",) + r[4:] if r[1] == "Convert" and r[2] == "BTC" else r for r in YEAR_2025
    ]
    with pytest.raises(ValueError, match="does not list both sides"):
        _import([_parse(rows=rows, holdings=dict(HOLDINGS_2025, BTC="0.033"))])


def test_every_transaction_becomes_a_mutation():
    btc = _securities(_import([_parse()]))["BTC"]
    mutations = [(s.referenceDate, s.quantity, s.name) for s in btc.stock if s.mutation]
    assert mutations == [
        (date(2025, 3, 3), Decimal("0.02"), "Buy"),
        (date(2025, 7, 1), Decimal("0.003"), "Convert"),
        (date(2025, 8, 1), Decimal("-0.001"), "Send"),
    ]


def test_reward_income_becomes_a_payment_in_usd():
    usdc = _securities(_import([_parse()]))["USDC"]
    assert [(p.paymentDate, p.amount, p.amountCurrency, p.quantity) for p in usdc.payment] == [
        (date(2025, 5, 1), Decimal("1.50"), "USD", Decimal("1.5")),
        (date(2025, 6, 1), Decimal("1.60"), "USD", Decimal("1.6")),
    ]


def test_fiat_wallet_balance_comes_from_the_summary_and_is_zero_when_not_listed():
    accounts = _import([_parse()]).listOfBankAccounts.bankAccount
    assert [(a.bankAccountCurrency, a.taxValue.balance) for a in accounts] == [
        ("EUR", Decimal("0"))
    ]


def test_listed_fiat_balance_is_used_and_implausible_balance_warns(caplog):
    holdings = dict(HOLDINGS_2025, EUR="500.00")
    with caplog.at_level(logging.WARNING):
        accounts = _import([_parse(holdings=holdings)]).listOfBankAccounts.bankAccount
    assert accounts[0].taxValue.balance == Decimal("500.00")
    assert "differs from the estimate" in caplog.text


def test_statement_for_another_period_is_rejected():
    with pytest.raises(ValueError, match="No Coinbase statement covers the tax period"):
        _import([_parse(period_start="2025-07-01 00:00:00 UTC", as_of="2025-07-31 23:59:59 UTC")])


def test_filtered_statement_is_rejected():
    with pytest.raises(ValueError, match="is filtered"):
        _import([_parse(filter_text="Asset: BTC")])


def test_unknown_transaction_type_stops_the_import():
    rows = YEAR_2025 + [
        (
            "2025-10-01 08:00:00 UTC",
            "Staking Income",
            "USDC",
            "1",
            "USD",
            "$1.00",
            "$0.00",
            "$1.00",
            "$1.00",
            "",
        )
    ]
    with pytest.raises(NotImplementedError, match="Staking Income"):
        _import([_parse(rows=rows, holdings=dict(HOLDINGS_2025, USDC="304.1"))])


def test_quantity_sign_contradicting_the_type_is_rejected():
    rows = [r if r[1] != "Send" else r[:3] + ("0.001",) + r[4:] for r in YEAR_2025]
    with pytest.raises(ValueError, match="unexpected quantity"):
        _import([_parse(rows=rows)])


def test_transactions_exceeding_the_holdings_are_rejected():
    with pytest.raises(ValueError, match="opening balance would be"):
        _import([_parse(holdings=dict(HOLDINGS_2025, USDC="100"))])


def test_csv_export_must_match_the_statement():
    ok = parse_transactions_csv(transactions_csv(), "export.csv")
    _import([_parse()], [("export.csv", ok)])
    partial = parse_transactions_csv(transactions_csv(YEAR_2025[:3]), "partial.csv")
    with pytest.raises(ValueError, match="does not contain the same transactions"):
        _import([_parse()], [("partial.csv", partial)])


def test_two_different_statements_for_the_year_are_rejected():
    other = _parse(holdings=dict(HOLDINGS_2025, USDT="101"))
    with pytest.raises(ValueError, match="Several different statements"):
        _import([_parse(), other])
