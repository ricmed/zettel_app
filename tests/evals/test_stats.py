"""Proportions, paired tests and power for the gold-set rounds (#332)."""

import random

import pytest
from zettel.evals.stats import (
    connor_items,
    items_for_power,
    mcnemar_power,
    paired_exact_p,
    paired_mid_p,
    wilson,
)

# -- Wilson ---------------------------------------------------------------


def test_wilson_is_bounded_and_empty_safe():
    assert wilson(0, 0) == (0.0, 0.0)
    low, high = wilson(0, 30)
    assert low == 0.0 and 0.0 < high < 0.15
    low, high = wilson(30, 30)
    assert 0.85 < low < 1.0 and high == 1.0


def test_wilson_matches_a_hand_computed_interval():
    """25/42: centre (p + z^2/2n) / (1 + z^2/n) = 0.5873, half-width 0.1423."""
    assert wilson(25, 42) == (0.4449, 0.7296)
    # #218 round 2 (25/42 vs 16/42) overlap, as p = 0.093 says; Jev 66/69 on #208.
    assert wilson(16, 42) == (0.25, 0.5319)
    assert wilson(66, 69) == (0.8798, 0.9851)


# -- exact and mid-p McNemar ---------------------------------------------


def test_paired_exact_matches_the_hand_computed_value():
    """17 items only one model gets right vs 6 the other does -- the #177 book split.

    2 * sum(C(23, i), i <= 6) / 2**23 = 2 * 145499 / 8388608.
    """
    assert paired_exact_p(17, 6) == pytest.approx(2 * 145499 / 2**23)
    assert paired_exact_p(17, 6) == pytest.approx(0.0347, abs=5e-4)


def test_mid_p_subtracts_the_probability_of_the_observed_split():
    """mid-p = exact - P(X = 6), X ~ Binomial(23, 1/2) = (2 * 145499 - 100947) / 2**23."""
    assert paired_mid_p(17, 6) == pytest.approx((2 * 145499 - 100947) / 2**23)
    assert paired_mid_p(17, 6) == pytest.approx(0.0227, abs=5e-4)
    # #218 round 2: 16 vs 7 -- 0.093 exact, under 0.07 mid-p, neither under 0.05.
    assert paired_exact_p(16, 7) == pytest.approx(0.0931, abs=5e-4)
    assert paired_mid_p(16, 7) == pytest.approx(0.0639, abs=5e-4)


@pytest.mark.parametrize("test", [paired_exact_p, paired_mid_p])
def test_paired_tests_are_symmetric_and_bounded(test):
    assert test(6, 17) == test(17, 6)
    assert test(5, 5) == 1.0
    assert test(0, 0) == 1.0
    assert 0.0 < test(10, 0) < 0.01


def test_mid_p_is_never_above_the_exact_p():
    for a in range(12):
        for b in range(12):
            assert paired_mid_p(a, b) <= paired_exact_p(a, b)


# -- power ---------------------------------------------------------------


def test_power_grows_with_the_round_and_with_the_effect():
    by_items = [mcnemar_power(n, 0.3, 0.1) for n in (50, 100, 200, 400)]
    assert by_items == sorted(by_items)
    assert by_items[-1] > 0.9
    by_effect = [mcnemar_power(100, 0.3, effect) for effect in (0.05, 0.1, 0.2)]
    assert by_effect == sorted(by_effect)


def test_power_matches_a_seeded_simulation():
    """Draw rounds of 100 items, count the ones the exact test adopts B on."""
    rng = random.Random(332)  # noqa: S311 -- a seeded simulation, not a secret
    n, discordance, effect, rounds = 100, 0.3, 0.1, 4000
    only_b, only_a = (discordance + effect) / 2, (discordance - effect) / 2
    adopted = 0
    for _ in range(rounds):
        draws = [rng.random() for _ in range(n)]
        b = sum(1 for u in draws if u < only_b)
        a = sum(1 for u in draws if only_b <= u < only_b + only_a)
        adopted += b > a and paired_exact_p(a, b) < 0.05
    assert adopted / rounds == pytest.approx(mcnemar_power(n, discordance, effect), abs=0.025)


def test_items_for_power_reaches_the_target_and_brackets_connor():
    """Connor (1987) for 0.3 / 0.1: ((1.96 * 0.5477 + 0.8416 * 0.5385) / 0.1)**2 = 233.1."""
    assert connor_items(0.3, 0.1) == 234
    exact = items_for_power(0.3, 0.1)
    mid = items_for_power(0.3, 0.1, mid_p=True)
    assert mcnemar_power(exact, 0.3, 0.1) >= 0.8
    assert mcnemar_power(exact - 1, 0.3, 0.1) < 0.8
    # The exact test is conservative, so it needs more items than the approximation.
    assert mid < connor_items(0.3, 0.1) < exact


def test_the_218_round_was_underpowered_for_its_own_effect():
    """16 vs 7 of 42: discordance 23/42, effect 9/42 -- the round could not decide."""
    assert mcnemar_power(42, 23 / 42, 9 / 42) < 0.5
    assert items_for_power(23 / 42, 9 / 42) > 2 * 42


def test_items_for_power_gives_up_above_the_cap():
    assert items_for_power(0.3, 0.01, cap=50) is None


@pytest.mark.parametrize(("discordance", "effect"), [(0.0, 0.0), (0.2, 0.3), (1.2, 0.1)])
def test_impossible_rounds_are_refused(discordance, effect):
    with pytest.raises(ValueError):
        mcnemar_power(50, discordance, effect)
