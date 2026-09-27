# Morgan Stanley at Work Importer Guide

This guide explains how to use stock plan data from
[Morgan Stanley at Work](https://atwork.morganstanley.com/) (the Shareworks
platform that replaced StockPlan Connect) with OpenSteuerAuszug. It was built
for Alphabet GSUs but works for any plan whose statements have the same
layout.

The importer is **experimental**: enable it with
`experimental_importers = true` in the `[general]` section of `config.toml`.

## Required Input: Quarterly Statement PDFs

The importer reads the **quarterly statements** issued by Morgan Stanley Smith
Barney. They are the only export that contains USD amounts together with
dividends, withholding tax and quarter-end balances.

> The *Activity Report* (XLSX/CSV) under Activity → Reports converts all
> amounts with a single display exchange rate, and it has no dividends. Do not
> use it for tax purposes.

### How to obtain the statements

1. Log in to Morgan Stanley at Work and open **Documents**.
2. Select the **Statements** filter.
3. Download the four statements **Quarterly Statement** dated 31-Mar,
   30-Jun, 30-Sep and 31-Dec of the tax year. The 31-Dec statement is
   usually posted in early February.
4. Put them into one directory per tax year. Other PDFs in the same directory
   (Account Summary, IRS Form 1042-S, duplicate downloads such as
   `... (1).pdf`) are ignored.

The statements must cover the whole tax year without gaps. The importer checks
that each quarter's closing share and cash balances equal the next quarter's
opening balances, and that all transactions reconcile with those balances.

### Supporting documents (recommended)

The generated Steuerauszug is created by you, not by Morgan Stanley, so keep
the originals and consider attaching them for the tax office (see
[Appending the originals](#appending-the-originals)):

* the four quarterly statements (above), and
* the **IRS Form 1042-S**, which proves the US tax withheld on dividends
  (needed for the foreign withholding tax credit, DA-1): **Documents** →
  **Download tax information** → *&lt;year&gt; tax year*. It downloads as a ZIP
  containing the 1042-S PDF.

Example layout:

```text
tax/2025/morganstanley/
├── quarterly_statement_2025-03-31.pdf
├── quarterly_statement_2025-06-30.pdf
├── quarterly_statement_2025-09-30.pdf
├── quarterly_statement_2025-12-31.pdf
└── form_1042s_2025.pdf          (ignored by the importer)
```

## Configuration (`config.toml`)

```toml
[general]
canton = "ZH"
full_name = "Erika Mustermann"
experimental_importers = true

[brokers.morganstanley.accounts.gsu]
account_number = "MS12345678"   # "Account Number" on the statement
# Only needed for plans the importer does not know (currently Alphabet
# Class A/C are built in):
# symbol = "GOOG"
# isin = "US02079K1079"
```

## Running OpenSteuerAuszug

The input is the **directory** with the statements, not a single file.

```console
# Once per tax year: fetch the official Kursliste
opensteuerauszug kursliste download --year 2025

opensteuerauszug process tax/2025/morganstanley/ --importer morganstanley --tax-year 2025 -o steuerauszug_ms_2025.pdf
```

Check the output for errors, and in the generated PDF check the positions,
the cash balance and the payment reconciliation table (dividends and
withholding against the Kursliste). Then import the PDF into your tax
software as described in the [User Guide](user_guide.md).

### Appending the originals

`--post-amble` appends PDFs after the generated statement (repeat it for
several files):

```console
opensteuerauszug process tax/2025/morganstanley/ --importer morganstanley --tax-year 2025 \
  --post-amble tax/2025/morganstanley/quarterly_statement_2025-03-31.pdf \
  --post-amble tax/2025/morganstanley/quarterly_statement_2025-06-30.pdf \
  --post-amble tax/2025/morganstanley/quarterly_statement_2025-09-30.pdf \
  --post-amble tax/2025/morganstanley/quarterly_statement_2025-12-31.pdf \
  --post-amble tax/2025/morganstanley/form_1042s_2025.pdf \
  -o steuerauszug_ms_2025.pdf
```

The [standalone web app](webapp.md) does not offer this importer yet; use the
command-line version.

## What is imported

| Statement row | Result |
|---|---|
| `Release <qty> <price>` | Vested shares deposited: security mutation at the vest price |
| `Release <amount>` | Sell-at-vest (trading plan) net proceeds: USD cash only |
| `Sale (<qty>) <price> <gross> <net>` | Security mutation (sale); net proceeds to USD cash |
| `Dividend Credit` | Dividend payment on the security |
| `Withholding Tax` | US withholding tax on the dividend |
| `Cancel Withholding Tax` | Refund, netted into the preceding withholding (matches the 1042-S) |
| `Proceeds Disbursement` | Cash wired out of the account |

Any other row type stops the import with an error so that nothing tax-relevant
is silently dropped. Please report such rows.

## Importer Specifics & Known Quirks

* **Vest income is not part of the Steuerauszug.** Income from vesting is
  reported on your salary certificate (Lohnausweis). The "STOCK OPTION AND
  AWARD ACTIVITY" section of the statement is therefore ignored.
* **Unvested GSUs are not reported.** They are not in the quarterly
  statements. Check how your canton wants them declared.
* **Fees** are not propagated to the Steuerauszug.
* The dividend withholding rate depends on your W-8BEN (15% with a valid
  treaty claim for Swiss residents). The payment reconciliation in the
  generated PDF flags mismatches.

## Tests and sample data

* `tests/importers/morganstanley/statement_text.py` holds a small synthetic
  tax year as extracted page text (four quarters that reconcile exactly). The
  parser and importer unit tests use it directly.
* `tests/samples/import/morganstanley/anonymized_2025/` contains four real
  2025 quarterly statements, anonymised in place: the original layout, fonts
  and encodings are kept, while the identity is fake and all quantities and
  amounts are re-randomised and recomputed (see below).
* `tests/test_integration_morganstanley_2025.py` runs the full CLI pipeline on
  those PDFs against the mini Kursliste (which includes Alphabet Class C with
  its real 2025 dividends) and checks XSD validity, dividend reconciliation,
  skipping of unrelated PDFs and appending of the originals.
* To test with your own statements without committing them, put one tax year
  per directory under `private/samples/import/morganstanley/<year>/` (or
  `$EXTRA_SAMPLE_DIR/import/morganstanley/<year>/`); the sample test picks them
  up automatically.

### Contributing anonymised statements

`scripts/anonymize_morganstanley_statements.py` turns a year of real quarterly
statements into shareable samples:

```console
uv run --with pikepdf python scripts/anonymize_morganstanley_statements.py OUT_DIR q1.pdf q2.pdf q3.pdf q4.pdf
```

It replaces name, address, mailing code, account number and grant IDs, draws
new random share quantities per grant (so no single scale factor can be
reversed), gives each vest date a new random payroll-tax rate, and recomputes
every derived amount so the statements still reconcile and the dividends still
match the Kursliste. Dates and prices (public market data) are kept. The random
values are never stored. At the end it searches the output for every original
identifier and changed number and fails if any is found. Still review the
result yourself: the timeline (vest dates, when shares were sold) is preserved.

---
Return to [User Guide](user_guide.md)
