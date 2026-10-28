"""Turn the portal's raw records into the clean domain model, and flag data problems."""
from collections import defaultdict

from .models import LEVELS, Geo, HierarchyLevel, HierarchyNode, Meter, Transformer


def _clean(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


Lookup = dict[str, tuple[dict[str, str], dict[str, str]]]  # level -> (name by code, code by name)


def _build_lookup(raws: list[dict]) -> Lookup:
    """Learn code<->name pairs from complete records, keeping only unambiguous ones."""
    seen: dict[str, tuple[dict[str, set], dict[str, set]]] = {lv: ({}, {}) for lv in LEVELS}
    for raw in raws:
        for level in LEVELS:
            node = raw.get("hierarchy", {}).get(level) or {}
            code, name = _clean(node.get("code")), _clean(node.get("name"))
            if code and name:
                seen[level][0].setdefault(code, set()).add(name)
                seen[level][1].setdefault(name, set()).add(code)
    return {
        level: (
            {k: next(iter(v)) for k, v in by_code.items() if len(v) == 1},
            {k: next(iter(v)) for k, v in by_name.items() if len(v) == 1},
        )
        for level, (by_code, by_name) in seen.items()
    }


def normalize_meter(raw: dict, lookup: Lookup | None = None) -> Meter:
    issues: list[str] = []
    levels: list[HierarchyLevel] = []
    path: list[str] = []
    for level in LEVELS:
        node = raw.get("hierarchy", {}).get(level) or {}
        code, name = _clean(node.get("code")), _clean(node.get("name"))
        name_by_code, code_by_name = (lookup or {}).get(level, ({}, {}))
        # The portal sometimes drops half of a code/name pair. Where other meters tell us
        # unambiguously what it was, fill it in (flagged as inferred) so the node isn't split in two.
        if code is None and name in code_by_name:
            code = code_by_name[name]
            issues.append(f"inferred_code:{level}")
        elif code is None:
            issues.append(f"missing_code:{level}")
        if name is None and code in name_by_code:
            name = name_by_code[code]
            issues.append(f"inferred_name:{level}")
        elif name is None:
            issues.append(f"missing_name:{level}")
        path.append(code or name or "unknown")
        levels.append(HierarchyLevel(level=level, code=code, name=name, node_id="/".join(path)))

    if levels[-1].code and levels[-1].code != raw.get("dtCode"):
        issues.append("dt_code_mismatch")

    return Meter(
        meter_id=raw["meterId"],
        serial_no=raw["serialNo"],
        make=raw["make"],
        phase_type=raw["phaseType"],
        install_status=raw["installStatus"],
        install_type=raw["installType"],
        build=raw.get("build", ""),
        dt_code=raw["dtCode"],
        geo=Geo(lat=raw["geo"]["lat"], lng=raw["geo"]["lng"]),
        hierarchy=levels,
        data_quality=issues,
    )


def normalize_meters(raws: list[dict]) -> list[Meter]:
    lookup = _build_lookup(raws)
    return [normalize_meter(raw, lookup) for raw in raws]


def build_tree(meters: list[Meter]) -> list[HierarchyNode]:
    """Group meters into a tree keyed by full path, since child codes are reused under different parents."""
    nodes: dict[str, HierarchyNode] = {}
    roots: list[HierarchyNode] = []
    for meter in meters:
        parent: HierarchyNode | None = None
        for lvl in meter.hierarchy:
            node = nodes.get(lvl.node_id)
            if node is None:
                node = HierarchyNode(
                    node_id=lvl.node_id, level=lvl.level, code=lvl.code, name=lvl.name, meter_count=0
                )
                nodes[lvl.node_id] = node
                (parent.children if parent else roots).append(node)
            node.meter_count += 1
            # Record each distinct problem once per node, not once per meter.
            for issue in meter.data_quality:
                if issue.endswith(f":{lvl.level}") and issue not in node.issues:
                    node.issues.append(issue)
            parent = node
    return roots


def build_transformers(raw_dts: list[dict], meters: list[Meter]) -> list[Transformer]:
    meter_count: dict[str, int] = defaultdict(int)
    feeders: dict[str, set[str | None]] = defaultdict(set)
    for meter in meters:
        meter_count[meter.dt_code] += 1
        feeders[meter.dt_code].add(meter.hierarchy[-2].code)  # feeder level
    return [
        Transformer(
            code=dt["code"],
            name=dt["name"],
            feeder_code=dt["feederCode"],
            capacity_kva=dt["capacityKva"],
            meter_count=meter_count[dt["code"]],
            feeder_matches_meters=feeders[dt["code"]] <= {dt["feederCode"]},
        )
        for dt in raw_dts
    ]
