# Coinbase Importer Guide

This guide explains how to use data from a plain [Coinbase](https://www.coinbase.com/)
account (the Coinbase app / coinbase.com) with OpenSteuerAuszug. Coinbase
Advanced, Coinbase Prime and self-custody wallets are not covered.

The importer is **experimental**: enable it with
`experimental_importers = true` in the `[general]` section of `config.toml`.

## How crypto is taxed and reported

* **Wealth tax:** holdings at 31 December are valued at the official ESTV
  Kursliste year-end value per coin. The Kursliste lists the major tokens
  (Bitcoin, Ethereum, USD Coin, Tether, ...) as `CURRNOTE.TOKEN` with a valor
  number; OpenSteuerAuszug finds them by ticker.
* **Income:** rewards (e.g. USDC rewards shown as "Reward Income") are taxable
  income. The Kursliste has no income entries for tokens, so the amounts come
  from Coinbase and are converted to CHF with the official exchange rate of
  the payment date. They are reported as income without Swiss withholding tax.
* **Purchases, sales, conversions, sends and receives** are listed as
  transactions. Capital gains of private investors are tax-free.
* **Fiat wallets** (e.g. EUR) are reported as bank accounts with their
  year-end balance.

## Required input: the yearly statement (HTML)

1. Log in to Coinbase and open **Statements** (profile menu → Statements, or
   `accounts.coinbase.com/statements`).
2. Under **Generate custom statement** choose **Asset: All assets**,
   **Transaction type: All transactions**, **Date: the tax year** (e.g. "2025")
   and **Format: HTML**, then **Generate** and download.
3. Optionally generate the same statement as **CSV**. If present, the importer
   checks that it contains exactly the same transactions.
4. Put the file(s) into one directory per tax year.

The HTML statement contains its date range, the filter and the **Portfolio
Summary** (holdings at the end of the period, "as of 2025-12-31 23:59:59 UTC").
The importer uses the statement whose period is exactly the tax year and
rejects filtered statements. Monthly statements in the same directory are
ignored.

Example layout:

```text
tax/2025/coinbase/
├── statement_2025.htm
└── transactions_2025.csv      (optional cross-check)
```

## Configuration (`config.toml`)

```toml
[general]
canton = "ZH"
full_name = "Erika Mustermann"
experimental_importers = true

[brokers.coinbase.accounts.main]
account_number = "COINBASE"   # Coinbase has no account number; used as identifier
```

## Running OpenSteuerAuszug

```console
opensteuerauszug kursliste download --year 2025
opensteuerauszug process tax/2025/coinbase/ --importer coinbase --tax-year 2025 -o steuerauszug_coinbase_2025.pdf
```

To attach the original statement, also download it as **PDF** and pass it with
`--post-amble statement_2025.pdf`.

## What is imported

| Transaction type | Result |
|---|---|
| `Buy`, `Receive` | Coins received (quantity must be positive) |
| `Sell`, `Send`, `Retail MGX DEX Send` | Coins leaving the account (quantity must be negative) |
| `Convert`, `Retail Mgx Dex Trade` | One row per side of the swap |
| `Reward Income` | Coins received **and** taxable income (value in USD at receipt) |
| `Deposit`, `Withdrawal` (fiat) | Only used to check the fiat balance for plausibility |

Any other transaction type stops the import with an error, so that nothing
tax-relevant is silently dropped. Please report such types.

## Checks

* For every coin, the opening balance derived backwards from the year-end
  holding must not be negative, and the balance may never go negative during
  the year. (Coinbase's transaction quantities already include network fees.)
* The statement must cover exactly the tax year and be unfiltered; a CSV export
  in the directory must match it.
* **Fiat balances** cannot be reconciled from the export: Coinbase lists fiat
  deposits, but purchases paid from the fiat wallet only appear with their USD
  value. The year-end balance is therefore taken from the Portfolio Summary (an
  asset that is not listed has a zero balance), and a warning is shown if it is
  far from the estimate "deposits minus wallet-funded purchases". If in doubt,
  check the balance in the wallet's history in the Coinbase app.

## Known limitations

* Dates are the UTC dates printed by Coinbase; an event shortly before midnight
  UTC on 31 December is 1 January in Swiss local time.
* Reward income is converted with the Kursliste exchange rate of the payment
  date (the monthly average when no daily rate is published).
* Coins that are not in the Kursliste are reported without a tax value (a
  warning is shown). Request their inclusion from the ESTV as described in the
  [User Guide](user_guide.md).
* All prices must be in one currency (the account's native currency, e.g. USD).

## Tests and sample data

* `tests/importers/coinbase/statement_data.py` generates a synthetic statement
  and CSV export (made-up values only) used by the unit tests and
  `tests/test_integration_coinbase_2025.py`, which runs the full pipeline
  against the mini Kursliste (with the Bitcoin, USD Coin and Tether tokens).
* To test with your own statements without committing them, put one tax year
  per directory under `private/samples/import/coinbase/<year>/`; the sample
  test picks them up automatically.

---
Return to [User Guide](user_guide.md)
