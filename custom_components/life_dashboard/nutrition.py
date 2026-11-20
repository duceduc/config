"""What was eaten today, from the meals the ledger holds.

The phone sends each meal (a NutritionRecord, or a food in HealthKit) with its calories,
protein, carbs and fat. history.py sums each of those per day, keyed by the meal's uuid,
so a meal sent again or edited replaces its own share. The sensors read today's sums from
there, so they are right after a restart and start again at 0 at midnight.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, tzinfo
from typing import Final

from .payload import (
    KEY_CALORIES_EATEN_TODAY,
    KEY_CARBS_TODAY,
    KEY_FAT_TODAY,
    KEY_PROTEIN_TODAY,
    SensorUpdate,
)

# Sensor key -> the ledger's day sum it reads.
NUTRITION_SENSORS: Final[dict[str, str]] = {
    KEY_CALORIES_EATEN_TODAY: "nutrition_calories",
    KEY_PROTEIN_TODAY: "nutrition_protein",
    KEY_CARBS_TODAY: "nutrition_carbs",
    KEY_FAT_TODAY: "nutrition_fat",
}


def nutrition_updates(
    day_totals: Callable[[str], dict[str, float]],
    *,
    today: date,
    tz: tzinfo,
    known: frozenset[str] = frozenset(),
) -> list[SensorUpdate]:
    """Today's four sums, for each one that has ever had a meal or already has a sensor.

    A phone that never sent a meal gets no sensors. Once one exists it keeps going, at 0 on
    a day without meals, rather than holding yesterday's figure.
    """
    midnight = datetime.combine(today, time.min, tzinfo=tz)
    updates = []
    for key, ledger_key in NUTRITION_SENSORS.items():
        totals = day_totals(ledger_key)
        if not totals and key not in known:
            continue
        updates.append(
            SensorUpdate(
                key=key,
                value=round(totals.get(today.isoformat(), 0.0), 1),
                measured_at=midnight,
                attributes={"date": today.isoformat()},
                last_reset=midnight,
            )
        )
    return updates
