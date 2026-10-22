"""Statistics shared by the gold-set scripts: proportions, paired tests and power (#332).

Stdlib only and never imported by the pipeline (ADR-038).

* ``wilson`` -- the 95% interval reported next to every proportion.
* ``paired_exact_p`` / ``paired_mid_p`` -- McNemar on the items exactly one of two
  conditions gets right. The exact conditional test is conservative: its actual
  size sits below alpha, so it misses real effects on small rounds. The mid-p
  subtracts half the probability of the observed split and keeps the type I error
  close to nominal in practice (Fagerland, Lydersen & Laake, BMC Med Res Methodol
  13:91, 2013). Reports show both; a pre-registered rule names the one it binds on.
* ``mcnemar_power`` / ``items_for_power`` -- how many items a round needs before
  labelling. Exact: the power is summed over every possible discordant count and
  split, not simulated. A round is planned from two numbers, the share of items the
  two conditions disagree on (``discordance``) and the accuracy difference worth
  detecting (``effect``); a previous round of the same decision gives both.
  ``connor_items`` is the normal approximation (Connor, Biometrics 43:207, 1987),
  printed beside the exact count as a sanity check.

Power counts only rejections in the direction of the effect: the rules in
``evals/preregistration/`` adopt a challenger when it is right more often *and*
p < alpha.
"""

from __future__ import annotations

import math
from statistics import NormalDist


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for ``k`` successes in ``n``."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def paired_exact_p(only_a_right: int, only_b_right: int) -> float:
    """Two-sided exact McNemar p-value over the discordant items."""
    n = only_a_right + only_b_right
    if n == 0:
        return 1.0
    k = min(only_a_right, only_b_right)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_mid_p(only_a_right: int, only_b_right: int) -> float:
    """Two-sided mid-p McNemar: the exact p minus the probability of the observed split."""
    n = only_a_right + only_b_right
    if n == 0:
        return 1.0
    k = min(only_a_right, only_b_right)
    below = sum(math.comb(n, i) for i in range(k))
    return min(1.0, (2 * below + math.comb(n, k)) / 2**n)


def _binom_pmf(n: int, p: float) -> list[float]:
    """P(X = k) for X ~ Binomial(n, p), k = 0..n, through log-gamma (no underflow at n=2000)."""
    if p <= 0.0:
        return [1.0] + [0.0] * n
    if p >= 1.0:
        return [0.0] * n + [1.0]
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n = math.lgamma(n + 1)
    return [
        math.exp(log_n - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * log_p + (n - k) * log_q)
        for k in range(n + 1)
    ]


def _conditional_power(max_discordant: int, toward_b: float, alpha: float, mid_p: bool) -> list:
    """P(reject in favour of B | d discordant items), d = 0..max_discordant.

    Given ``d``, the p-value depends only on the smaller side ``k``, and grows with it,
    so the test rejects for B exactly when B's side is at least ``d - k_max``.
    """
    out = [0.0]
    for d in range(1, max_discordant + 1):
        null = _binom_pmf(d, 0.5)
        k_max, below = -1, 0.0
        for k in range((d - 1) // 2 + 1):
            p_value = 2 * below + (null[k] if mid_p else 2 * null[k])
            if p_value >= alpha:
                break
            k_max, below = k, below + null[k]
        if k_max < 0:
            out.append(0.0)
            continue
        split = _binom_pmf(d, toward_b)
        out.append(sum(split[d - k_max :]))
    return out


def _split(discordance: float, effect: float) -> float:
    if not 0.0 < discordance <= 1.0 or not 0.0 < effect <= discordance:
        raise ValueError("esperado 0 < efeito <= discordancia <= 1")
    return (discordance + effect) / (2 * discordance)


def mcnemar_power(
    n: int, discordance: float, effect: float, alpha: float = 0.05, *, mid_p: bool = False
) -> float:
    """Power of the paired test on ``n`` items to find B better by ``effect``.

    ``discordance`` = P(exactly one condition right); ``effect`` = accuracy(B) -
    accuracy(A). B alone is right with probability ``(discordance + effect) / 2``.
    """
    conditional = _conditional_power(n, _split(discordance, effect), alpha, mid_p)
    return sum(w * r for w, r in zip(_binom_pmf(n, discordance), conditional, strict=True))


def items_for_power(
    discordance: float,
    effect: float,
    power: float = 0.8,
    alpha: float = 0.05,
    *,
    mid_p: bool = False,
    cap: int = 1000,
) -> int | None:
    """Smallest number of items whose power reaches ``power``; ``None`` above ``cap``.

    The exact test's power is a sawtooth in ``n``, so a round a few items larger can
    fall back under the target; plan on this number, not on a smaller one.
    """
    conditional = _conditional_power(cap, _split(discordance, effect), alpha, mid_p)
    for n in range(1, cap + 1):
        achieved = sum(
            w * r for w, r in zip(_binom_pmf(n, discordance), conditional[: n + 1], strict=True)
        )
        if achieved >= power:
            return n
    return None


def connor_items(discordance: float, effect: float, power: float = 0.8, alpha: float = 0.05) -> int:
    """Connor's (1987) normal approximation of the items a paired test needs."""
    _split(discordance, effect)
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_beta = NormalDist().inv_cdf(power)
    root = z_alpha * math.sqrt(discordance) + z_beta * math.sqrt(discordance - effect**2)
    return math.ceil((root / effect) ** 2)
