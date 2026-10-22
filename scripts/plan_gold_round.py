"""How many items a gold-set round needs before anyone labels it (#332).

A round compares two conditions with the paired McNemar test. Its power depends on
two numbers: the share of items exactly one condition gets right (`--discordance`)
and the accuracy difference worth detecting (`--effect`). Both can be read off a
previous round of the same decision (`--from-round N:ONLY_A:ONLY_B`, the counts
`score_decision_gold.py` reports as `n`, `only_a_right`, `only_b_right`), which
plans the next round to detect an effect as large as the one already seen.

Prints the items needed for the target power with the exact test and with the
mid-p, Connor's (1987) approximation beside them, and, with `--items`, the power a
round of that size has. Offline and deterministic: stdlib arithmetic, no file read.

Usage:
    .venv/Scripts/python.exe scripts/plan_gold_round.py --discordance 0.3 --effect 0.1
    .venv/Scripts/python.exe scripts/plan_gold_round.py --from-round 42:7:16 --items 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.evals.stats import connor_items, items_for_power, mcnemar_power


def from_round(spec: str) -> tuple[float, float]:
    """``N:ONLY_A:ONLY_B`` -> (discordance, effect) of B over A."""
    try:
        n, only_a, only_b = (int(part) for part in spec.split(":"))
    except ValueError:
        raise ValueError(f"esperado N:SO_A:SO_B, recebido {spec!r}") from None
    if n <= 0 or only_a < 0 or only_b <= only_a or only_a + only_b > n:
        raise ValueError(f"rodada sem efeito a favor de B: {spec!r}")
    return (only_a + only_b) / n, (only_b - only_a) / n


def plan(
    discordance: float,
    effect: float,
    power: float = 0.8,
    alpha: float = 0.05,
    items: int | None = None,
    cap: int = 1000,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "discordance": round(discordance, 4),
        "effect": round(effect, 4),
        "power": power,
        "alpha": alpha,
        "items_exact": items_for_power(discordance, effect, power, alpha, cap=cap),
        "items_mid_p": items_for_power(discordance, effect, power, alpha, mid_p=True, cap=cap),
        "items_connor": connor_items(discordance, effect, power, alpha),
    }
    if items is not None:
        out["at_items"] = {
            "items": items,
            "power_exact": round(mcnemar_power(items, discordance, effect, alpha), 4),
            "power_mid_p": round(mcnemar_power(items, discordance, effect, alpha, mid_p=True), 4),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--discordance", type=float, help="P(so uma condicao acerta)")
    source.add_argument("--from-round", help="N:SO_A:SO_B de uma rodada anterior")
    parser.add_argument("--effect", type=float, help="acuracia(B) - acuracia(A)")
    parser.add_argument("--power", type=float, default=0.8)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--items", type=int, default=None, help="poder de uma rodada deste tamanho")
    parser.add_argument("--cap", type=int, default=1000)
    args = parser.parse_args(argv)

    if args.from_round:
        discordance, effect = from_round(args.from_round)
    elif args.effect is None:
        parser.error("--discordance pede --effect")
    else:
        discordance, effect = args.discordance, args.effect

    result = plan(discordance, effect, args.power, args.alpha, args.items, args.cap)
    cap_note = f"> {args.cap}"
    print(f"discordancia {result['discordance']:.1%}  efeito {result['effect']:+.1%}")
    print(f"itens para poder {args.power:.0%} com alfa {args.alpha}:")
    print(f"  McNemar exato  {result['items_exact'] or cap_note}")
    print(f"  mid-p          {result['items_mid_p'] or cap_note}")
    print(f"  Connor (1987)  {result['items_connor']}")
    if "at_items" in result:
        at = result["at_items"]
        print(
            f"poder com {at['items']} itens: exato {at['power_exact']:.0%}, "
            f"mid-p {at['power_mid_p']:.0%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
