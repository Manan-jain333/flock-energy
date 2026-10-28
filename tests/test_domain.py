from datetime import datetime

from app.consumption import IST, build_consumption, parse_readings
from app.normalize import build_tree, normalize_meters


def raw_meter(meter_id, zone=("Z-01", "Jaipur Zone 1"), circle=("C-01", "Circle 1"), division=("D-01", "Division 1")):
    def node(pair):
        return {"code": pair[0], "name": pair[1]}

    return {
        "meterId": meter_id, "serialNo": "S" + meter_id, "make": "HPL", "phaseType": "single",
        "installStatus": "Installed", "installType": "CT Operated", "build": "legacy", "dtCode": "DT-001",
        "geo": {"lat": 26.9, "lng": 75.8},
        "hierarchy": {
            "zone": node(zone), "circle": node(circle), "division": node(division),
            "subdivision": node(("SD-01", "Subdivision 1")), "substation": node(("SS-01", "Substation 1")),
            "feeder": node(("F-001", "Feeder 1")), "dt": node(("DT-001", "DT 1")),
        },
    }


def test_missing_code_is_inferred_from_other_meters_and_flagged():
    meters = normalize_meters([raw_meter("A"), raw_meter("B", circle=("", "Circle 1"))])
    b = meters[1]
    assert b.hierarchy[1].code == "C-01"
    assert "inferred_code:circle" in b.data_quality
    assert meters[0].hierarchy[1].node_id == b.hierarchy[1].node_id  # same node, not split in two


def test_uninferable_gap_is_reported_not_hidden():
    (m,) = normalize_meters([raw_meter("A", circle=("", "Circle 9"))])
    assert m.hierarchy[1].code is None
    assert m.hierarchy[1].node_id.endswith("/Circle 9")
    assert "missing_code:circle" in m.data_quality


def test_reused_child_codes_under_different_parents_stay_separate():
    meters = normalize_meters(
        [raw_meter("A", circle=("C-01", "Circle 1")), raw_meter("B", circle=("C-03", "Circle 3"))]
    )
    tree = build_tree(meters)
    divisions = [d.node_id for c in tree[0].children for d in c.children]
    assert divisions == ["Z-01/C-01/D-01", "Z-01/C-03/D-01"]
    assert tree[0].meter_count == 2


def rows(*values):
    return [
        {"timestamp": f"24/06/2026 {h:02d}:{m:02d}", "kwh": kwh, "kvah": "1", "voltR": "230"}
        for (h, m, kwh) in values
    ]


def test_interval_kwh_uses_register_differences():
    r = parse_readings(rows((0, 0, "100.0"), (0, 30, "100.5"), (1, 0, "101.25")))
    assert [x.interval_kwh for x in r] == [None, 0.5, 0.75]
    assert r[0].timestamp == datetime(2026, 6, 24, 0, 0, tzinfo=IST)


def test_register_reset_and_blank_values_are_flagged_not_summed():
    r = parse_readings(rows((0, 0, "100"), (0, 30, "5"), (1, 0, ""), (1, 30, "6")))
    assert r[1].flags == ["register_reset"] and r[1].interval_kwh is None
    assert "missing_kwh" in r[2].flags
    assert r[3].interval_kwh == 1  # measured against the last good reading (the reset one)


def test_readings_are_sorted_and_duplicates_collapsed():
    r = parse_readings(rows((1, 0, "2"), (0, 0, "1"), (0, 0, "1")))
    assert [x.timestamp.hour for x in r] == [0, 1]
    assert "duplicate_timestamp" in r[0].flags


def test_window_keeps_deltas_computed_from_the_full_series():
    data = rows((0, 0, "100"), (1, 0, "101"), (2, 0, "103"))
    c = build_consumption("M", data, start=datetime(2026, 6, 24, 1, 0, tzinfo=IST))
    assert [x.interval_kwh for x in c.readings] == [1, 2]  # first in-window reading still has a delta
    assert c.summary.total_kwh == 3
    assert c.summary.typical_interval_minutes == 60


def test_daily_aggregate():
    data = [
        {"timestamp": "24/06/2026 23:00", "kwh": "10", "kvah": "1", "voltR": "230"},
        {"timestamp": "25/06/2026 00:00", "kwh": "12", "kvah": "1", "voltR": "230"},
        {"timestamp": "25/06/2026 12:00", "kwh": "15", "kvah": "1", "voltR": "230"},
    ]
    c = build_consumption("M", data, aggregate="daily")
    assert c.readings is None
    assert [(d.date, d.kwh) for d in c.daily] == [("2026-06-25", 5)]
