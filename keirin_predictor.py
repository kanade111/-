"""Keirin (Japanese track cycling) two-rider exacta predictor and data fetcher.

The predictor combines four rider features that are published ahead of races:

* race score (競走得点)
* upset level (波乱度, a larger value means the rider is less predictable)
* riding style (脚質)
* line strength (ラインパワー)

It can operate on CSV files or, with the ``--fetch`` flag, download the daily
race cards from public APIs. Live downloads target the Rakuten K-Dreams feed and
automatically fall back to a bundled offline dataset when the remote server is
unreachable, ensuring predictions remain available even in restricted
environments.

Example usage::

    $ python keirin_predictor.py sample_riders.csv --top 3
    $ python keirin_predictor.py --fetch --top 3

"""

from __future__ import annotations

import argparse
import datetime as _dt
import csv
import itertools
from math import exp
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from keirin_fetcher import DataSource, fetch_racecards
from keirin_models import DEFAULT_LEG_TYPES, Rider


class KeirinPredictor:
    """Heuristic model for ２車複 predictions."""

    #: Pair-style synergy multipliers.  Values above 1.0 reward compatible
    #: combinations, while values below 1.0 penalise awkward ones.
    #:
    #: The dictionary is symmetric: order of the key does not matter.
    SYNERGY: Dict[Tuple[str, str], float] = {
        ("逃げ", "追込"): 1.15,
        ("逃げ", "捲り"): 1.05,
        ("逃げ", "自在"): 1.05,
        ("捲り", "追込"): 1.1,
        ("捲り", "自在"): 1.05,
        ("追込", "自在"): 1.08,
    }

    #: Upset level weight.  Larger values increase the penalty for unstable
    #: riders.
    UPSET_WEIGHT: float = 0.6

    #: Line power weight.  Controls how much the reported ラインパワー influences
    #: pair and line scoring.  The upstream feed commonly uses 0-100 values.
    LINE_POWER_WEIGHT: float = 0.4

    #: Penalty applied to pairs coming from different lines.  The penalty is
    #: softened when both riders still have high individual line power.
    LINE_MISMATCH_WEIGHT: float = 0.25

    #: Label used when a rider is listed as "単騎" (no line affiliation).
    SOLO_LABEL: str = "単騎"

    def __init__(self, leg_types: Sequence[str] = DEFAULT_LEG_TYPES) -> None:
        self.leg_types = tuple(leg_types)

    def predict_pairs(self, riders: Sequence[Rider], top: int = 5) -> List[Tuple[Tuple[Rider, Rider], float]]:
        """Return ranked two-rider combinations.

        Parameters
        ----------
        riders:
            Rider data for the race.
        top:
            Number of pairs to return.
        """

        pairs = []
        for r1, r2 in itertools.combinations(riders, 2):
            score = self._pair_score(r1, r2)
            pairs.append(((r1, r2), score))

        # Convert raw scores into pseudo-probabilities using a softmax.
        if not pairs:
            return []

        raw_scores = [score for (_, score) in pairs]
        max_score = max(raw_scores)
        exp_scores = [exp(s - max_score) for s in raw_scores]
        total = sum(exp_scores)
        probabilities = [s / total for s in exp_scores]

        ranked = [
            (pairs[i][0], probabilities[i])
            for i in sorted(range(len(pairs)), key=lambda idx: raw_scores[idx], reverse=True)
        ]
        return ranked[:top]

    def _pair_score(self, rider_a: Rider, rider_b: Rider) -> float:
        """Combine rider attributes into a single score.

        The score is a product of three elements:
        1. Base performance: sum of the riders' racing scores.
        2. Stability: a penalty that reduces the score when the riders have
           high upset levels.
        3. Style synergy: a multiplier that rewards complementary riding styles.
        """

        base = rider_a.score + rider_b.score
        stability = self._pair_stability(rider_a, rider_b)
        synergy = self._style_synergy(rider_a.leg_type, rider_b.leg_type)
        line_factor = self._line_factor(rider_a, rider_b)
        return base * stability * synergy * line_factor

    def _rider_stability(self, rider: Rider) -> float:
        return max(0.2, 1.0 - rider.upset * self.UPSET_WEIGHT)

    def _pair_stability(self, rider_a: Rider, rider_b: Rider) -> float:
        return (self._rider_stability(rider_a) + self._rider_stability(rider_b)) / 2

    def _style_synergy(self, style_a: str, style_b: str) -> float:
        if style_a == style_b:
            return 1.02  # similar riders can cooperate but with modest gain.

        key = tuple(sorted((style_a, style_b)))
        return self.SYNERGY.get(key, 1.0)

    def _line_factor(self, rider_a: Rider, rider_b: Rider) -> float:
        norm_a = self._normalized_line_power(rider_a)
        norm_b = self._normalized_line_power(rider_b)
        if rider_a.line and rider_b.line and rider_a.line == rider_b.line:
            avg = (norm_a + norm_b) / 2
            return 1.0 + avg * self.LINE_POWER_WEIGHT

        shared = (norm_a + norm_b) / 2
        penalty = 1.0 - (0.5 - shared) * self.LINE_MISMATCH_WEIGHT
        return max(0.7, penalty)

    def _normalized_line_power(self, rider: Rider) -> float:
        if rider.line_power is None:
            # Riders with an assigned line but missing power get a neutral boost,
            # while "単騎" riders skew slightly below average.
            return 0.55 if rider.line else 0.4

        value = max(0.0, min(float(rider.line_power), 100.0))
        return value / 100.0

    def predict_lines(
        self, riders: Sequence[Rider], top: int = 3
    ) -> List[Tuple[str, Sequence[Rider], float]]:
        """Return ranked line combinations based on ラインパワー and stability."""

        line_groups: Dict[str, List[Rider]] = {}
        display_labels: Dict[str, str] = {}

        for rider in riders:
            line_name = (rider.line or "").strip()
            if not line_name:
                key = f"{self.SOLO_LABEL}:{rider.name}"
                display = f"{self.SOLO_LABEL}({rider.name})"
            else:
                key = line_name
                display = line_name

            line_groups.setdefault(key, []).append(rider)
            display_labels[key] = display

        if not line_groups:
            return []

        scores: List[float] = []
        ordered_keys: List[str] = []
        for key, members in line_groups.items():
            ordered_keys.append(key)
            base = sum(r.score for r in members)
            stability = sum(self._rider_stability(r) for r in members) / max(1, len(members))
            power = sum(self._normalized_line_power(r) for r in members) / max(1, len(members))
            score = base * stability * (1.0 + power * self.LINE_POWER_WEIGHT)
            scores.append(score)

        max_score = max(scores)
        exp_scores = [exp(s - max_score) for s in scores]
        total = sum(exp_scores)
        probabilities = [s / total for s in exp_scores]

        ranked = [
            (display_labels[ordered_keys[i]], line_groups[ordered_keys[i]], probabilities[i])
            for i in sorted(range(len(ordered_keys)), key=lambda idx: scores[idx], reverse=True)
        ]
        return ranked[:top]


def load_riders(path: Path) -> List[Rider]:
    """Load rider data from a CSV file.

    The CSV file must contain the columns ``name``, ``score``, ``upset`` and
    ``leg_type``.  Optional fields ``line`` and ``line_power`` provide additional
    information for line-based predictions.  Numeric values can be given as
    integers or floats.
    """

    riders: List[Rider] = []
    with path.open(newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        missing = {field for field in ("name", "score", "upset", "leg_type") if field not in reader.fieldnames}
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            try:
                line = row.get("line", "").strip() if reader.fieldnames and "line" in reader.fieldnames else ""
                line_power_value = row.get("line_power") if reader.fieldnames and "line_power" in reader.fieldnames else None
                line_power = None
                if line_power_value not in (None, ""):
                    line_power = float(line_power_value)
                riders.append(
                    Rider(
                        name=row["name"].strip(),
                        score=float(row["score"]),
                        upset=float(row["upset"]),
                        leg_type=row["leg_type"].strip() or "自在",
                        line=line or None,
                        line_power=line_power,
                    )
                )
            except ValueError as exc:  # pragma: no cover - illustrative error handling.
                raise ValueError(f"Invalid numeric value in row: {row}") from exc
    return riders


def _describe_rider(rider: Rider) -> str:
    line = rider.line or KeirinPredictor.SOLO_LABEL
    return f"{rider.name} [{line}/{rider.leg_type}]"


def format_prediction(pair: Tuple[Rider, Rider], probability: float) -> str:
    rider_a, rider_b = pair
    percentage = probability * 100
    return f"{_describe_rider(rider_a)} - {_describe_rider(rider_b)}: {percentage:.1f}%"


def format_line_prediction(line_name: str, riders: Sequence[Rider], probability: float) -> str:
    percentage = probability * 100
    members = ", ".join(_describe_rider(r) for r in riders)
    return f"{line_name}: {percentage:.1f}% -> {members}"


def _parse_date(date_text: str) -> _dt.date:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(date_text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        "Date must be formatted as YYYY-MM-DD, YYYY/MM/DD, or YYYYMMDD"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Two-rider Keirin quinella predictor. Provide a CSV file or use --fetch "
            "to download race cards from online or bundled sources."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="CSV file containing rider data (name, score, upset, leg_type)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Number of combinations to show (default: 5)",
    )
    parser.add_argument(
        "--top-lines",
        type=int,
        default=3,
        help="Number of line combinations to show (default: 3)",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=(
            "Download race cards for the selected date (defaults to today) and "
            "predict each race"
        ),
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        help="Target date when using --fetch (default: today)",
    )
    parser.add_argument(
        "--data-source",
        choices=[DataSource.AUTO, DataSource.RAKUTEN, DataSource.LOCAL],
        default=DataSource.AUTO,
        help=(
            "Race-card provider when using --fetch. 'auto' tries the live "
            "Rakuten feed then falls back to the offline dataset, 'rakuten' "
            "forces the live API, and 'local' always uses the bundled sample."
        ),
    )
    args = parser.parse_args(argv)

    if args.fetch:
        if args.input is not None:
            parser.error("CSV input should not be provided together with --fetch")
        target_date = args.date or _dt.date.today()
        try:
            race_cards = fetch_racecards(target_date, source=args.data_source)
        except Exception as exc:
            raise SystemExit(f"Failed to obtain race cards: {exc}") from exc
        if not race_cards:
            raise SystemExit(
                f"No race cards were found for {target_date.isoformat()}"
            )

        predictor = KeirinPredictor()
        for card in race_cards:
            print(
                f"\n{card.venue_name} {card.race_number}R {card.race_title}"
                f" (race ID: {card.race_id})"
            )
            predictions = predictor.predict_pairs(card.riders, top=args.top)
            if not predictions:
                print("  No riders available")
                continue
            for pair, probability in predictions:
                print("  - " + format_prediction(pair, probability))
            line_predictions = predictor.predict_lines(card.riders, top=args.top_lines)
            if line_predictions:
                print("  Line predictions:")
                for line_name, members, probability in line_predictions:
                    print("    - " + format_line_prediction(line_name, members, probability))
    else:
        if args.input is None:
            parser.error("Either supply a CSV file or use --fetch")

        riders = load_riders(args.input)
        if len(riders) < 2:
            raise SystemExit("At least two riders are required")

        predictor = KeirinPredictor()
        predictions = predictor.predict_pairs(riders, top=args.top)

        print(f"Top {len(predictions)} two-rider combinations:")
        for pair, probability in predictions:
            print("  - " + format_prediction(pair, probability))
        line_predictions = predictor.predict_lines(riders, top=args.top_lines)
        if line_predictions:
            print("\nTop line predictions:")
            for line_name, members, probability in line_predictions:
                print("  - " + format_line_prediction(line_name, members, probability))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point.
    raise SystemExit(main())
