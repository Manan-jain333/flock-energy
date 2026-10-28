from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]


class HierarchyLevel(BaseModel):
    level: str = Field(description="One of zone, circle, division, subdivision, substation, feeder, dt.")
    code: str | None = Field(description="Null when the portal has no code for this level.")
    name: str | None = Field(description="Null when the portal has no name for this level.")
    node_id: str = Field(
        description="Path-style id of this node in /hierarchy (codes repeat under different parents, "
        "so a code alone is not unique)."
    )


class Geo(BaseModel):
    lat: float
    lng: float


class Meter(BaseModel):
    meter_id: str
    serial_no: str
    make: str
    phase_type: Literal["single", "three"] | str
    install_status: str
    install_type: str
    build: str = Field(description="Portal record format ('legacy' or 'v2'); kept for traceability.")
    dt_code: str
    geo: Geo
    hierarchy: list[HierarchyLevel] = Field(description="Ordered zone -> dt.")
    data_quality: list[str] = Field(
        default_factory=list, description="Problems detected in the portal's data for this meter."
    )


class MeterPage(BaseModel):
    items: list[Meter]
    total: int
    limit: int
    offset: int


class NearbyMeter(BaseModel):
    distance_km: float
    meter: Meter


class NearbyResult(BaseModel):
    center: Geo
    radius_km: float
    items: list[NearbyMeter]


class HierarchyNode(BaseModel):
    node_id: str
    level: str
    code: str | None
    name: str | None
    meter_count: int = Field(description="Meters at or below this node.")
    issues: list[str] = Field(default_factory=list)
    children: list["HierarchyNode"] = []


class Transformer(BaseModel):
    code: str
    name: str
    feeder_code: str
    capacity_kva: int
    meter_count: int
    feeder_matches_meters: bool = Field(
        description="False if meters on this DT report a different feeder than the DT registry."
    )


class Reading(BaseModel):
    timestamp: datetime = Field(description="ISO 8601, assumed Asia/Kolkata (+05:30).")
    kwh: float | None = Field(description="Cumulative active-energy register.")
    kvah: float | None = Field(description="Cumulative apparent-energy register.")
    voltage_r: float | None = Field(description="R-phase voltage in volts.")
    interval_kwh: float | None = Field(
        description="kWh consumed since the previous reading; null for the first reading, "
        "missing data, or a register reset."
    )
    flags: list[str] = Field(default_factory=list)


class DailyUsage(BaseModel):
    date: str
    kwh: float
    readings: int


class ConsumptionSummary(BaseModel):
    reading_count: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    typical_interval_minutes: float | None = Field(description="Median gap between readings.")
    total_kwh: float = Field(description="Sum of valid interval deltas.")
    min_voltage: float | None
    max_voltage: float | None


class Consumption(BaseModel):
    meter_id: str
    summary: ConsumptionSummary
    readings: list[Reading] | None = Field(description="Present for aggregate=raw.")
    daily: list[DailyUsage] | None = Field(description="Present for aggregate=daily.")
