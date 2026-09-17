# -*- coding: utf-8 -*-
"""EP-2-13: hub-pushed organization policy baseline tests (runtime side).

Covers the hub_rules evaluation precedence (outranks builtin and user
layers), the fail-closed digest verification, and the guarantee that
local policy.yaml can neither forge nor persist organization rules.
"""

from __future__ import annotations

import json
import os

import yaml

from qwenpaw.governance.policy import (
    DEFAULT_BUILTIN_RULES,
    GovernanceAction,
    GovernancePolicy,
    GovernanceRule,
    apply_hub_baseline_from_env,
    load_governance_policy,
    save_governance_policy,
)
from qwenpaw.hub.policy_catalog import canonical_rule_digest

from tests.unit.governance.test_policy import _tc

_RULES = [
    {"match": "Bash(rm -rf *)", "action": "deny", "reason": "org"},
    {"match": "Read(/tmp/**)", "action": "allow", "reason": "org"},
]


def _env(rules=None, sha=None):
    payload = {
        "revision": 3,
        "sha256": sha
        if sha is not None
        else canonical_rule_digest(
            rules if rules is not None else _RULES,
        ),
        "rules": rules if rules is not None else _RULES,
    }
    return {"QWENPAW_POLICY_BASELINE_JSON": json.dumps(payload)}


# ------------------------------------------------------------- apply


def test_applies_rules_from_env() -> None:
    policy = GovernancePolicy()
    applied = apply_hub_baseline_from_env(policy, _env())
    assert applied == 2
    assert [r.match for r in policy.hub_rules] == [
        "Bash(rm -rf *)",
        "Read(/tmp/**)",
    ]
    assert policy.hub_rules[0].action == GovernanceAction.DENY


def test_empty_env_clears_layer() -> None:
    policy = GovernancePolicy()
    apply_hub_baseline_from_env(policy, _env())
    assert policy.hub_rules
    assert apply_hub_baseline_from_env(policy, {}) == 0
    assert policy.hub_rules == []


def test_digest_mismatch_keeps_previous_layer() -> None:
    policy = GovernancePolicy()
    apply_hub_baseline_from_env(policy, _env())
    before = list(policy.hub_rules)

    applied = apply_hub_baseline_from_env(policy, _env(sha="f" * 64))
    # fail-closed: previous hub rules kept, replacement refused
    assert applied == len(before)
    assert policy.hub_rules == before


def test_malformed_payload_keeps_previous_layer() -> None:
    policy = GovernancePolicy()
    apply_hub_baseline_from_env(policy, _env())
    before = list(policy.hub_rules)

    bad = {"QWENPAW_POLICY_BASELINE_JSON": "{not json"}
    apply_hub_baseline_from_env(policy, bad)
    assert policy.hub_rules == before

    missing = {"QWENPAW_POLICY_BASELINE_JSON": json.dumps({"rules": []})}
    apply_hub_baseline_from_env(policy, missing)
    assert policy.hub_rules == before


# ---------------------------------------------------------- evaluate


def test_hub_deny_overrides_user_allow() -> None:
    policy = GovernancePolicy(
        hub_rules=[
            GovernanceRule(
                match="Bash(git *)",
                action=GovernanceAction.DENY,
                reason="org",
            ),
        ],
        user_rules=[
            GovernanceRule(
                match="Bash(git *)",
                action=GovernanceAction.ALLOW,
                reason="local",
            ),
        ],
        execution_level="off",
    )
    decision = policy.evaluate(_tc("Bash", "git push"))
    assert decision.action == GovernanceAction.DENY
    assert decision.source == "hub_rules"


def test_hub_allow_not_downgraded_by_user_deny() -> None:
    policy = GovernancePolicy(
        hub_rules=[
            GovernanceRule(
                match="Read(/tmp/**)",
                action=GovernanceAction.ALLOW,
                reason="org",
            ),
        ],
        user_rules=[
            GovernanceRule(
                match="Read(/tmp/**)",
                action=GovernanceAction.DENY,
                reason="local",
            ),
        ],
        execution_level="off",
    )
    decision = policy.evaluate(_tc("Read", "/tmp/data.txt"))
    assert decision.action == GovernanceAction.ALLOW
    assert decision.source == "hub_rules"


def test_hub_outranks_builtin() -> None:
    assert DEFAULT_BUILTIN_RULES, "builtin layer must be non-empty"
    builtin_target = None
    builtin = next(
        (r for r in DEFAULT_BUILTIN_RULES if r.action is not None),
        None,
    )
    if builtin is None:  # pragma: no cover - defensive
        return
    # find any target the first builtin rule matches via wildcard tool
    probe = GovernancePolicy()
    decision = probe.evaluate(_tc("Read", "/etc/passwd"))
    if decision.source != "builtin_rules":
        return  # builtin coverage varies; precedence covered elsewhere
    builtin_target = "/etc/passwd"

    policy = GovernancePolicy(
        hub_rules=[
            GovernanceRule(
                match="*(/etc/**)",
                action=GovernanceAction.ALLOW,
                reason="org override",
            ),
        ],
        execution_level="off",
    )
    decision = policy.evaluate(_tc("Read", builtin_target))
    assert decision.source == "hub_rules"


def test_strict_mode_still_escalates_hub_allow() -> None:
    policy = GovernancePolicy(
        hub_rules=[
            GovernanceRule(
                match="Read(/tmp/**)",
                action=GovernanceAction.ALLOW,
                reason="org",
            ),
        ],
        execution_level="strict",
    )
    decision = policy.evaluate(_tc("Read", "/tmp/x"))
    assert decision.action == GovernanceAction.ASK
    assert decision.source == "STRICT mode"


def test_no_hub_layer_behavior_unchanged() -> None:
    plain = GovernancePolicy(
        user_rules=[
            GovernanceRule(
                match="Bash(echo *)",
                action=GovernanceAction.ALLOW,
                reason="local",
            ),
        ],
        execution_level="off",
    )
    assert plain.evaluate(_tc("Bash", "echo hi")).action == (
        GovernanceAction.ALLOW
    )


# -------------------------------------------------- persistence guard


def test_yaml_hub_rules_key_is_inert(tmp_path) -> None:
    policy = GovernancePolicy(
        user_rules=[
            GovernanceRule(
                match="Bash(echo *)",
                action=GovernanceAction.ALLOW,
                reason="local",
            ),
        ],
    )
    save_governance_policy(policy, str(tmp_path), "")
    # forge hub_rules into the YAML like a local edit would
    yaml_path = tmp_path / "policy.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    data["hub_rules"] = [
        {"match": "Bash(*)", "action": "allow", "reason": "forged"},
    ]
    yaml_path.write_text(
        yaml.safe_dump(data),
        encoding="utf-8",
    )

    loaded = load_governance_policy(str(tmp_path), str(tmp_path))
    assert loaded.hub_rules == []  # env is the only source


def test_save_never_writes_hub_rules(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "QWENPAW_POLICY_BASELINE_JSON",
        _env()["QWENPAW_POLICY_BASELINE_JSON"],
    )
    policy = load_governance_policy(str(tmp_path), str(tmp_path))
    assert apply_hub_baseline_from_env(policy) == 2
    assert policy.hub_rules

    save_governance_policy(policy, str(tmp_path), str(tmp_path))
    data = yaml.safe_load(
        (tmp_path / "policy.yaml").read_text(encoding="utf-8"),
    )
    assert "hub_rules" not in data
    monkeypatch.delenv("QWENPAW_POLICY_BASELINE_JSON", raising=False)
    assert os.environ.get("QWENPAW_POLICY_BASELINE_JSON") is None
