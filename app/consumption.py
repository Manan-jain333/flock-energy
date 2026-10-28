"""Parse the portal's energy readings and derive per-interval usage."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from .models import Consumption, ConsumptionSummary, DailyUsage, Reading

# The portal gives naive "DD/MM/YYYY HH:mm" strings. India has no DST, so a fixed
# offset is exact for this utility; it is an assumption, documented in the README.
IST = timezone(timedelta(hours=5, minutes=30))


def _number(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def parse_readings(rows: list[dict]) -> list[Reading]:
    parsed: dict[datetime, Reading] = {}
    for row in rows:
        try:
            ts = datetime.strptime(row["timestamp"], "%d/%m/%Y %H:%M").replace(tzinfo=IST)
        except (KeyError, ValueError):
            continue  # a reading without a usable timestamp can't be placed on a timeline
        flags = []
        values = {}
        for src, dst in (("kwh", "kwh"), ("kvah", "kvah"), ("voltR", "voltage_r")):
            values[dst] = _number(row.get(src))
            if values[dst] is None:
                flags.append(f"missing_{dst}")
        if ts in parsed:
            flags.append("duplicate_timestamp")  # last one wins
        parsed[ts] = Reading(timestamp=ts, interval_kwh=None, flags=flags, **values)

    readings = [parsed[ts] for ts in sorted(parsed)]
    previous: Reading | None = None
    for reading in readings:
        if reading.kwh is not None:
            if previous is not None:
                delta = reading.kwh - previous.kwh
                if delta < 0:
                    reading.flags.append("register_reset")
                else:
                    reading.interval_kwh = round(delta, 3)
            previous = reading
    return readings


def build_consumption(
    meter_id: str,
    rows: list[dict],
    start: datetime | None = None,
    end: datetime | None = None,
    aggregate: str = "raw",
) -> Consumption:
    readings = parse_readings(rows)  # deltas are computed on the full series, then windowed
    readings = [
        r for r in readings if (start is None or r.timestamp >= start) and (end is None or r.timestamp <= end)
    ]
    gaps = [(b.timestamp - a.timestamp).total_seconds() / 60 for a, b in zip(readings, readings[1:])]
    voltages = [r.voltage_r for r in readings if r.voltage_r is not None]
    summary = ConsumptionSummary(
        reading_count=len(readings),
        first_timestamp=readings[0].timestamp if readings else None,
        last_timestamp=readings[-1].timestamp if readings else None,
        typical_interval_minutes=median(gaps) if gaps else None,
        total_kwh=round(sum(r.interval_kwh or 0 for r in readings), 3),
        min_voltage=min(voltages, default=None),
        max_voltage=max(voltages, default=None),
    )
    if aggregate == "daily":
        per_day: dict[str, list[float]] = defaultdict(list)
        for r in readings:
            if r.interval_kwh is not None:
                per_day[r.timestamp.date().isoformat()].append(r.interval_kwh)
        daily = [DailyUsage(date=d, kwh=round(sum(v), 3), readings=len(v)) for d, v in sorted(per_day.items())]
        return Consumption(meter_id=meter_id, summary=summary, readings=None, daily=daily)
    return Consumption(meter_id=meter_id, summary=summary, readings=readings, daily=None)
