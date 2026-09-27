"""Synthetic, anonymised page text mimicking Morgan Stanley quarterly statements.

The layout follows what ``pypdf`` extracts from real "STATEMENT For the
Period ..." PDFs: a first-page summary and a SHARE PURCHASE AND HOLDINGS
list, followed by the STOCK OPTION AND AWARD ACTIVITY section that the
parser must ignore.
"""

from typing import List, Sequence

_SUMMARY = """STATEMENT For the Period {period}
Morgan Stanley Smith Barney LLC. Member SIPC.
*00  QSPSTT0000000000*
Erika Mustermann
MUSTERSTRASSE 1
ZURICH  8000
SWITZERLAND
Plan Details:
Plan Number: 99X
Company Name: Alphabet, Inc.
Issuer Description: {issuer}
Account Number: MS00000001
Share Purchase and Holdings Summary
Opening Value
(as of {opening})
Closing Value
(as of {closing})
Number of Shares {shares_open} {shares_close}
Share Price $100.0000 $110.0000
Share Value $0.00 $0.00
Cash Value {cash_open} {cash_close}
Net Unsettled Cash $0.00 $0.00
Total Account Value $0.00 $0.00
The quarter-end market closing price is utilized to calculate the Share Value.
"""

_ACTIVITY_HEADER = """STATEMENT For the Period {period} Page 3 of 4
Account Number: MS00000001
SHARE PURCHASE AND HOLDINGS
Transaction Date Activity Type Quantity Price
Gross
 Amount Total Taxes and Fees Total Net Amount
"""

_AWARD_SECTION = """STOCK OPTION AND AWARD ACTIVITY
Activity / Settlement
10/25/25
10/29/25
Release C1000000
RST $250.0000
12.000
12.000
$3,000.00 $1,000.00 $2,000.00
"""


def statement_pages(
    *,
    period: str,
    opening: str,
    closing: str,
    shares: Sequence[str],
    cash: Sequence[str],
    rows: Sequence[str],
    issuer: str = "ALPHABET INC CL C",
    with_award_section: bool = False,
) -> List[str]:
    first = _SUMMARY.format(
        period=period,
        issuer=issuer,
        opening=opening,
        closing=closing,
        shares_open=shares[0],
        shares_close=shares[1],
        cash_open=cash[0],
        cash_close=cash[1],
    )
    legal = "STATEMENT For the Period {0} Page 2 of 4\nQuestions?\n".format(period)
    activity = _ACTIVITY_HEADER.format(period=period) + "\n".join(rows) + "\n"
    activity += "Sell Transactions are provided as of trade date.\n"
    if with_award_section:
        activity += _AWARD_SECTION
    return [first, legal, activity]


# A full synthetic year: shares vest in Q1/Q2, are sold in Q3, sell-at-vest
# cash releases follow, and all cash is disbursed by year end.
#
# Dividends are consistent with the 2025 Kursliste entry for Alphabet Class C
# (USD 0.20 ex 2025-03-10, USD 0.21 ex 2025-06-09) so the end-to-end test can
# reconcile them: 30.5 shares -> 6.10, 40.5 shares -> 8.51 (rounded).  Q1 is
# withheld at 30% and partially refunded in Q2, leaving the 15% treaty rate.
Q1_2025 = statement_pages(
    period="January 1 — March 31, 2025",
    opening="1/1/25",
    closing="3/31/25",
    shares=("10.000", "30.500"),
    cash=("$100.00", "$104.27"),
    rows=[
        "1/25/25 Release 12.500 $200.0000  ",
        "2/25/25 Release 8.000 180.0000  ",
        "3/17/25 Dividend Credit $6.10 $6.10",
        "3/17/25 Withholding Tax (1.83)",
    ],
)
Q2_2025 = statement_pages(
    period="April 1 — June 30, 2025",
    opening="4/1/25",
    closing="6/30/25",
    shares=("30.500", "40.500"),
    cash=("$104.27", "$112.42"),
    rows=[
        "4/25/25 Release 10.000 $160.0000  ",
        "4/29/25 Cancel Withholding Tax $0.92",
        "6/16/25 Dividend Credit $8.51 8.51",
        "6/16/25 Withholding Tax (1.28)",
    ],
)
Q3_2025 = statement_pages(
    period="July 1 — September 30, 2025",
    opening="7/1/25",
    closing="9/30/25",
    shares=("40.500", "0.000"),
    cash=("$112.42", "$1,500.00"),
    rows=[
        "8/1/25 Sale (40.500) 190.0000 $7,695.00 $7,695.00",
        "8/4/25 Proceeds Disbursement (7,807.42)",
        "8/25/25 Release 1,500.00",
    ],
    with_award_section=True,
)
Q4_2025 = statement_pages(
    period="October 1 — December 31, 2025",
    opening="10/1/25",
    closing="12/31/25",
    shares=("0.000", "0.000"),
    cash=("$1,500.00", "$0.00"),
    rows=[
        "10/1/25 Proceeds Disbursement $(1,500.00)",
        "10/25/25 Release 2,000.00",
        "12/30/25 Proceeds Disbursement (2,000.00)",
    ],
    with_award_section=True,
)
