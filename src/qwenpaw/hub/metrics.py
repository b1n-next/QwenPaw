# -*- coding: utf-8 -*-
"""Hand-rolled Prometheus text collector for the hub (07 §4, EP-2-4).

No third-party client library: counters and gauges are plain dicts
behind a lock, rendered to the Prometheus exposition format on
request. Event-driven families (request decisions, quota soft
warnings) are incremented at the gates; store-backed families
(runtime states, usage totals, collector freshness) are sampled
into gauges when /metrics is scraped.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Tuple

_Labels = Tuple[Tuple[str, str], ...]


def _escape_label(value: str) -> str:
    """Escape a label value per the exposition format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_family(
    name: str,
    samples: Iterable[Tuple[_Labels, float]],
    kind: str,
    help_text: str,
) -> List[str]:
    """Render one metric family (HELP/TYPE header + samples)."""
    lines = [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} {kind}",
    ]
    for sorted_labels, value in sorted(samples):
        if sorted_labels:
            rendered = ",".join(
                f'{label}="{_escape_label(val)}"'
                for label, val in sorted_labels
            )
            lines.append(f"{name}{{{rendered}}} {value}")
        else:
            lines.append(f"{name} {value}")
    lines.append("")
    return lines


class HubMetrics:
    """Thread-safe in-process metric registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[_Labels, float]] = {}
        self._gauges: Dict[str, Dict[_Labels, float]] = {}

    # -------------------------------------------------- ingestion

    @staticmethod
    def _key(labels: Dict[str, str] | None) -> _Labels:
        return tuple(sorted((labels or {}).items()))

    def inc(
        self,
        family: str,
        value: float = 1.0,
        **labels: str,
    ) -> None:
        """Add to a counter family (empty labels allowed)."""
        key = self._key(labels)
        with self._lock:
            bucket = self._counters.setdefault(family, {})
            bucket[key] = bucket.get(key, 0.0) + value

    def set_gauge(
        self,
        family: str,
        value: float,
        **labels: str,
    ) -> None:
        """Overwrite one gauge sample (same label set replaces)."""
        key = self._key(labels)
        with self._lock:
            self._gauges.setdefault(family, {})[key] = value

    def clear_gauge_family(self, family: str) -> None:
        """Drop every sample of a gauge family (stale states leave)."""
        with self._lock:
            self._gauges.pop(family, None)

    # -------------------------------------------------- rendering

    def render(self) -> str:
        """Render the full exposition payload."""
        with self._lock:
            families: List[str] = []
            for name in sorted(self._counters):
                samples = list(self._counters[name].items())
                families.extend(
                    _render_family(
                        name,
                        samples,
                        "counter",
                        "monotonic counter",
                    ),
                )
            for name in sorted(self._gauges):
                samples = list(self._gauges[name].items())
                families.extend(
                    _render_family(
                        name,
                        samples,
                        "gauge",
                        "point-in-time value",
                    ),
                )
        return "\n".join(families).rstrip("\n") + "\n"

    # -------------------------------------------------- helpers

    def snapshot_counter(
        self,
        family: str,
        **labels: str,
    ) -> float:
        """Read one counter sample (test/display helper)."""
        with self._lock:
            return self._counters.get(family, {}).get(
                self._key(labels),
                0.0,
            )

    def snapshot_gauge(
        self,
        family: str,
        **labels: str,
    ) -> float:
        """Read one gauge sample (test/display helper)."""
        with self._lock:
            return self._gauges.get(family, {}).get(
                self._key(labels),
                0.0,
            )

    def reset(self) -> None:
        """Clear all samples (tests only)."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


__all__ = ["HubMetrics"]
