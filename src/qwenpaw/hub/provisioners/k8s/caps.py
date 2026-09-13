# -*- coding: utf-8 -*-
"""Capability declaration for the K8s runtime boundary (06 §4).

Phase 1 only declares and validates capabilities; the negotiation
engine (soft-degrade vs hard-refuse) lands in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeCaps:
    """Static capability statement of one runtime backend."""

    isolation: str  # container | pod-ns | microvm
    network: str  # none | egress-only | cluster-local | full
    filesystem: str  # tenant-pvc | tenant-dir | shared
    gpu: bool


K8S_RUNTIME_CAPS = RuntimeCaps(
    isolation="pod-ns",
    network="cluster-local",
    filesystem="tenant-pvc",
    gpu=False,
)


__all__ = ["K8S_RUNTIME_CAPS", "RuntimeCaps"]
