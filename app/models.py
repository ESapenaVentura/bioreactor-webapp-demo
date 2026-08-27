from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Reading:
    reactor: str        # "Cytiva Wave"
    category: str       # "Time Series"
    sensor: str         # "Temperature"
    value: float
    ts: datetime        # SourceTimestamp, tz-aware UTC
    status_ok: bool

    @property
    def key(self) -> str:
        """Stable id used as a dict key and sent to the browser."""
        return f"{self.reactor}|{self.sensor}"