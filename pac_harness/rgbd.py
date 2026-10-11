"""Internal metric RGB-D snapshot contract; arrays stay out of model JSON."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Observation:
    id: str
    captured_monotonic: float
    pose: list[float]  # Robot-base XYZ metres, extrinsic xyz radians.
    stamp_ns: int  # Capture wall-clock timestamp on the observation host.
    images: dict[str, Any]
    depths: dict[str, Any]  # Native unaligned floating point depth, metres.
    metadata: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
