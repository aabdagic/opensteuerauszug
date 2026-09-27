"""Crypto tokens (CURRNOTE.TOKEN) are found by ticker and priced without an ISIN."""

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.core.kursliste_manager import KurslisteManager
from scripts.convert_kursliste_to_sqlite import convert_kursliste_xml_to_sqlite

MINI_2025 = (
    Path(__file__).resolve().parents[1] / "samples" / "kursliste" / "kursliste_mini_2025.xml"
)


def _accessor(directory: Path):
    manager = KurslisteManager()
    manager.load_directory(directory)
    return manager.get_kurslisten_for_year(2025)


@pytest.fixture(params=["xml", "sqlite"])
def accessor(request, tmp_path):
    if request.param == "xml":
        shutil.copy(MINI_2025, tmp_path / "kursliste_2025.xml")
    else:
        convert_kursliste_xml_to_sqlite(str(MINI_2025), str(tmp_path / "kursliste_2025.sqlite"))
    return _accessor(tmp_path)


def test_token_is_found_by_ticker_case_insensitively(accessor):
    btc = accessor.get_token_by_symbol("btc")
    assert btc is not None
    assert (btc.securityName, int(btc.valorNumber), btc.isin) == ("Bitcoin", 39714275, None)


def test_unknown_ticker_finds_nothing(accessor):
    assert accessor.get_token_by_symbol("NOPE") is None


def test_ticker_of_a_share_is_not_mistaken_for_a_token(accessor):
    # Only CURRNOTE.TOKEN entries are searched; securityAppendix of shares is ignored.
    assert accessor.get_token_by_symbol("Namenaktien") is None


def test_token_year_end_price_is_available_without_isin(accessor):
    usdc = accessor.get_token_by_symbol("USDC")
    assert KurslisteManager.price_of(usdc) == Decimal("0.791977")


def test_ambiguous_ticker_is_not_guessed(tmp_path):
    xml = MINI_2025.read_text(encoding="utf-8")
    duplicate = (
        '<currencyNote id="999999" valorNumber="99999999" securityGroup="CURRNOTE" '
        'securityType="CURRNOTE.TOKEN" securityName="Other BTC" securityAppendix="BTC" '
        'country="XV" currency="XXX" denomination="1"/>\n  <currencyNote id="1177373"'
    )
    (tmp_path / "kursliste_2025.xml").write_text(
        xml.replace('<currencyNote id="1177373"', duplicate, 1), encoding="utf-8"
    )
    assert _accessor(tmp_path).get_token_by_symbol("BTC") is None
