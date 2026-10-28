import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    portal_base_url: str
    portal_username: str
    portal_password: str
    catalog_ttl: float = 300.0
    energy_ttl: float = 300.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            portal_base_url=os.environ.get("PORTAL_BASE_URL", "https://urja-ops.flockenergy.tech").rstrip("/"),
            portal_username=os.environ.get("PORTAL_USERNAME", ""),
            portal_password=os.environ.get("PORTAL_PASSWORD", ""),
            catalog_ttl=float(os.environ.get("CATALOG_TTL_SECONDS", "300")),
            energy_ttl=float(os.environ.get("ENERGY_TTL_SECONDS", "300")),
        )
