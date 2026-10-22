"""Tests for the gold-round planner (#332)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from plan_gold_round import from_round, main, plan


def test_a_previous_round_gives_discordance_and_effect():
    """#218 round 2: 7 vs 16 discordant of 42 items."""
    assert from_round("42:7:16") == pytest.approx((23 / 42, 9 / 42))


@pytest.mark.parametrize("spec", ["42:7", "x:1:2", "42:16:7", "42:7:7", "10:6:7"])
def test_a_round_without_an_effect_for_b_is_refused(spec):
    with pytest.raises(ValueError):
        from_round(spec)


def test_plan_reports_both_tests_connor_and_the_power_at_a_size():
    result = plan(0.3, 0.1, items=100)
    assert (result["items_exact"], result["items_connor"]) == (249, 234)
    assert result["items_mid_p"] < result["items_exact"]
    assert result["at_items"]["power_exact"] < result["at_items"]["power_mid_p"] < 0.8


def test_cli_prints_the_plan(capsys):
    assert main(["--from-round", "42:7:16", "--items", "42"]) == 0
    out = capsys.readouterr().out
    assert "McNemar exato  98" in out
    assert "poder com 42 itens" in out
