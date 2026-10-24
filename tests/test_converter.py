#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tester for braiins_mining_kryptosekken.py."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import braiins_mining_kryptosekken as conv  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "braiins_payouts_example.csv"

EXPECTED_HEADER = (
    "Tidspunkt,Type,Inn,Inn-Valuta,Ut,Ut-Valuta,Gebyr,Gebyr-Valuta,Marked,Notat"
)


def read_rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def tx_id(note: str) -> str:
    """Plukker ut transaction_id fra Notat-feltet."""
    match = re.search(r"tx ([0-9a-zA-Z]+)", note)
    return match.group(1) if match else ""


def run(tmp_path: Path, *extra: str) -> Path:
    out = tmp_path / "out.csv"
    code = conv.main(["-i", str(EXAMPLE), "-o", str(out), *extra])
    assert code == 0
    return out


def test_header_is_byte_identical(tmp_path):
    out = run(tmp_path)
    assert out.read_text(encoding="utf-8").splitlines()[0] == EXPECTED_HEADER


def test_duplicates_are_dropped(tmp_path):
    out = run(tmp_path)
    rows = read_rows(out)
    assert len(rows) == 3
    ids = [tx_id(r["Notat"]) for r in rows]
    assert sorted(ids) == ["aaaa1111", "bbbb2222", "cccc3333"]


def test_utc_is_converted_to_oslo_with_dst(tmp_path):
    rows = read_rows(run(tmp_path))
    by_id = {tx_id(r["Notat"]): r["Tidspunkt"] for r in rows}
    # Januar: CET = UTC+1
    assert by_id["aaaa1111"] == "2026-01-10 10:06:07"
    # April: CEST = UTC+2 (sommertid startet 29. mars 2026)
    assert by_id["bbbb2222"] == "2026-04-01 11:06:24"


def test_default_books_income_on_receiving_wallet(tmp_path):
    rows = read_rows(run(tmp_path))
    for row in rows:
        assert row["Type"] == "Mining"
        assert row["Inn-Valuta"] == "BTC"
        assert row["Ut"] == ""
        assert row["Ut-Valuta"] == ""
        assert row["Gebyr"] == ""
    assert rows[0]["Marked"] == "Alby"
    assert rows[0]["Inn"] == "0.00012345"
    assert rows[2]["Marked"] == "Strike"


def test_at_pool_with_transfer_adds_three_rows_per_payout(tmp_path):
    rows = read_rows(run(tmp_path, "--at-pool", "--transfer"))
    assert len(rows) == 9
    mining = [r for r in rows if r["Type"] == "Mining"]
    assert {r["Marked"] for r in mining} == {"Braiins-Pool"}
    transfers_out = [r for r in rows if r["Type"] == "Overføring-Ut"]
    transfers_in = [r for r in rows if r["Type"] == "Overføring-Inn"]
    assert len(transfers_out) == len(transfers_in) == 3
    # Samme beløp forlater poolen og kommer inn på lommeboken.
    assert [r["Ut"] for r in transfers_out] == [r["Inn"] for r in transfers_in]
    # For hver utbetaling kommer inntekten før overføringen.
    types_first_three = [r["Type"] for r in rows[:3]]
    assert types_first_three == ["Mining", "Overføring-Ut", "Overføring-Inn"]


def test_net_amount_is_default_and_fee_is_ignored(tmp_path):
    rows = read_rows(run(tmp_path))
    september = [r for r in rows if "cccc3333" in r["Notat"]][0]
    assert september["Inn"] == "0.00034567"
    assert "gebyr 0.00001 BTC" in september["Notat"]


def test_gross_fee_books_expense_row(tmp_path):
    rows = read_rows(run(tmp_path, "--gross-fee"))
    assert len(rows) == 4
    september = [r for r in rows if "cccc3333" in r["Notat"] and r["Type"] == "Mining"]
    assert september[0]["Inn"] == "0.00035567"
    expenses = [r for r in rows if r["Type"] == "Forvaltningskostnad"]
    assert len(expenses) == 1
    assert expenses[0]["Ut"] == "0.00001"
    assert expenses[0]["Ut-Valuta"] == "BTC"


def test_missing_required_column_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("account,target,date\nx,y,2026-01-01 00:00:00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="amount"):
        conv.read_payouts(bad)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        conv.read_payouts(tmp_path / "nope.csv")


def test_amounts_follow_the_import_spec(tmp_path):
    rows = read_rows(run(tmp_path, "--at-pool", "--transfer"))
    number = re.compile(r"^\d+\.\d+$")
    for row in rows:
        for column in ("Inn", "Ut"):
            if row[column]:
                assert number.match(row[column]), row[column]
            else:
                assert row[column + "-Valuta"] == ""
    assert all("Z" not in row["Tidspunkt"] for row in rows)
    assert all(
        len(row["Tidspunkt"]) == len("2026-01-10 10:06:07") for row in rows
    )


def test_marked_values_are_valid_kryptosekken_markets(tmp_path):
    """Marked godtar bare 1-16 tegn av A-Z, a-z, 0-9 og bindestrek."""
    rows = read_rows(run(tmp_path, "--at-pool", "--transfer"))
    for row in rows:
        assert conv.MARKET_PATTERN.match(row["Marked"]), row["Marked"]


def test_sanitize_market_removes_spaces_and_parentheses():
    assert conv.sanitize_market("Alby (testuser)") == "Alby-testuser"
    assert conv.sanitize_market("Braiins Pool") == "Braiins-Pool"
    assert conv.sanitize_market("Primal  (testlongusername)") == "Primal-testlongu"
    assert len(conv.sanitize_market("x" * 40)) == 16
    assert conv.sanitize_market("   ") == "Ukjent"


def test_wallet_label_for_lightning_and_onchain_targets():
    assert conv.wallet_label("testuser@getalby.com") == "Alby"
    assert conv.wallet_label("otheruser@strike.me") == "Strike"
    assert conv.wallet_label("bc1qexampleaddress000000") == "bc1qexamplea"
    assert conv.wallet_label("") == "Ukjent"


def test_colliding_targets_get_unique_labels():
    used = {}
    assert conv.unique_label("Alby", used, "a@getalby.com") == "Alby"
    assert conv.unique_label("Alby", used, "b@getalby.com") == "Alby-2"


def test_custom_pool_name_with_space_is_sanitized(tmp_path):
    rows = read_rows(run(tmp_path, "--at-pool", "--pool-name", "My Pool"))
    assert {r["Marked"] for r in rows} == {"My-Pool"}
    assert "My Pool" in rows[0]["Notat"]


def test_notes_are_safe_by_default(tmp_path):
    """Notat inneholder bare tegn Kryptosekken godtar: ingen @ eller |."""
    rows = read_rows(run(tmp_path, "--at-pool", "--transfer"))
    for row in rows:
        assert not conv.NOTE_ALLOWED.search(row["Notat"]), row["Notat"]
        assert "@" not in row["Notat"]
        assert len(row["Notat"]) <= conv.NOTE_MAX_LENGTH


def test_full_note_style_keeps_address(tmp_path):
    rows = read_rows(run(tmp_path, "--note-style", "full"))
    assert "testuser@getalby.com" in rows[0]["Notat"]


def test_note_sanitizer_removes_forbidden_characters():
    assert conv.sanitize_note("a@b.com | tx 123") == "ab.com tx 123"
    assert conv.sanitize_note("Prøve  med   æøå") == "Prøve med æøå"
    long = conv.sanitize_note("x" * 300, max_length=50)
    assert len(long) <= 50 and long.endswith("...")


def test_note_max_length_flag(tmp_path):
    rows = read_rows(run(tmp_path, "--note-max-length", "40"))
    assert all(len(r["Notat"]) <= 40 for r in rows)
