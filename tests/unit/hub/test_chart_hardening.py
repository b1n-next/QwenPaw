# -*- coding: utf-8 -*-
"""Render-time contract tests for the production-hardening chart
additions (K8s checklist ticket: PDB, probes, securityContext,
NetworkPolicy, Ingress, metrics scrape credentials)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parents[3] / "deploy" / "helm" / "qwenpaw-hub"


def _render(*args: str) -> str:
    result = subprocess.run(
        ["helm", "template", "qh", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _render_prod(extra: str = "") -> str:
    return _render(
        "-f",
        str(CHART / "values-prod.yaml"),
        "--set",
        "hub.adminPassword=smoke",
        *extra.split(),
    )


class ChartHardeningTest(unittest.TestCase):
    def test_prod_enables_liveness_and_startup_probes(self) -> None:
        out = _render_prod()
        self.assertIn("startupProbe:", out)
        self.assertIn("livenessProbe:", out)
        self.assertIn("readinessProbe:", out)

    def test_prod_hardens_the_container(self) -> None:
        out = _render_prod()
        self.assertIn("readOnlyRootFilesystem: true", out)
        self.assertIn("allowPrivilegeEscalation: false", out)
        self.assertIn("drop:", out)
        self.assertIn("ALL", out.split("drop:")[1][:20])
        self.assertIn("emptyDir: {}", out)  # /tmp survives RO rootfs
        # the image's working dirs move onto the PVC via subPath
        self.assertIn("subPath: working.secret", out)

    def test_default_keeps_optional_blocks_off(self) -> None:
        out = _render("--set", "hub.adminPassword=smoke")
        self.assertNotIn("kind: PodDisruptionBudget", out)
        self.assertNotIn("kind: Ingress", out)
        self.assertNotIn("kind: NetworkPolicy", out)
        # liveness/startup are on by default but hardening contexts stay
        # writable so older images boot unchanged
        self.assertNotIn("readOnlyRootFilesystem: true", out)

    def test_pdb_single_replica_allows_disruption_explicitly(self) -> None:
        out = _render(
            "--set",
            "hub.adminPassword=smoke",
            "--set",
            "podDisruptionBudget.enabled=true",
        )
        # single replica: the budget exists to make drain print an
        # explicit allowance, not to fake availability
        self.assertIn("minAvailable: 0", out)

    def test_pdb_multi_replica_keeps_one_writer(self) -> None:
        out = _render(
            "--set",
            "hub.adminPassword=smoke",
            "--set",
            "podDisruptionBudget.enabled=true",
            "--set",
            "hub.replicas=3",
        )
        self.assertIn("minAvailable: 2", out)

    def test_antiaffinity_only_above_one_replica(self) -> None:
        base = (
            "--set hub.adminPassword=smoke --set podAntiAffinity.enabled=true"
        )
        single = _render(*base.split())
        self.assertNotIn("podAntiAffinity:", single)
        multi = _render(*(base + " --set hub.replicas=2").split())
        self.assertIn("podAntiAffinity:", multi)
        self.assertIn("kubernetes.io/hostname", multi)

    def test_ingress_tls_requires_secret_name(self) -> None:
        out = _render(
            "--set",
            "hub.adminPassword=smoke",
            "--set",
            "ingress.enabled=true",
            "--set",
            "ingress.tls.enabled=true",
            "--set",
            "ingress.tls.secretName=hub-tls",
        )
        self.assertIn("kind: Ingress", out)
        self.assertIn('secretName: "hub-tls"', out)
        with self.assertRaises(subprocess.CalledProcessError):
            _render(
                "--set",
                "hub.adminPassword=smoke",
                "--set",
                "ingress.enabled=true",
                "--set",
                "ingress.tls.enabled=true",
            )

    def test_network_policy_widens_only_through_values(self) -> None:
        out = _render(
            "--set",
            "hub.adminPassword=smoke",
            "--set",
            "networkPolicy.enabled=true",
            "--set-string",
            "networkPolicy.allowedNamespaces={ingress-nginx}",
            "--set-string",
            "networkPolicy.allowedCIDRs={10.0.0.0/8}",
        )
        self.assertEqual(out.count("kind: NetworkPolicy"), 2)
        # ingress allowlist carries the namespace and CIDR
        self.assertIn("ingress-nginx", out)
        self.assertIn("10.0.0.0/8", out)
        # egress pins DNS + runtime ns + API server
        self.assertIn("port: 53", out)
        self.assertIn("name: qwenpaw-runtimes", out)

    def test_service_account_token_stays_mounted(self) -> None:
        # the K8s runtime provisioner needs the API; the flag is
        # explicit so a future default flip cannot break provisioning
        out = _render_prod()
        self.assertIn("automountServiceAccountToken: true", out)


if __name__ == "__main__":
    unittest.main()
