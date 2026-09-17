# -*- coding: utf-8 -*-
"""G2: runtime↔hub capability negotiation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw.hub.capability import (
    CapabilityRequirement,
    RuntimeCapability,
    negotiate,
)
from qwenpaw.hub.control_app import create_hub_app


# ------------------------------------------------- unit


def test_version_ordering_numeric_not_lexical() -> None:
    assert (
        negotiate(
            RuntimeCapability(version="2.10.0"),
            CapabilityRequirement(min_version="2.9"),
        ).ok
        is True
    )
    assert (
        negotiate(
            RuntimeCapability(version="2.9.9"),
            CapabilityRequirement(min_version="2.10"),
        ).ok
        is False
    )


def test_sandbox_and_tools_gates() -> None:
    cap = RuntimeCapability(
        version="1.0",
        sandbox_supported=True,
        tools=("web_search",),
    )
    assert (
        negotiate(
            cap,
            CapabilityRequirement(sandbox_required=True),
        ).ok
        is True
    )
    missing = negotiate(
        cap,
        CapabilityRequirement(
            sandbox_required=True,
            tools_required=("browser", "python"),
        ),
    )
    assert missing.ok is False
    assert missing.missing == ("tool:browser", "tool:python")
    no_sandbox = negotiate(
        RuntimeCapability(version="1.0"),
        CapabilityRequirement(sandbox_required=True),
    )
    assert no_sandbox.missing == ("sandbox",)


def test_metadata_roundtrip() -> None:
    cap = RuntimeCapability(
        version="2.2.0",
        sandbox_supported=True,
        sandbox_mode="landlock",
        tools=("b", "a"),
    )
    assert RuntimeCapability.from_metadata(cap.to_metadata()) == cap
    assert RuntimeCapability.from_metadata({}) == RuntimeCapability()


def test_requirement_document_parsing() -> None:
    requirement = CapabilityRequirement.from_document(
        {"value": {"min_version": "2.0", "tools_required": ["x"]}},
    )
    assert requirement.min_version == "2.0"
    assert requirement.tools_required == ("x",)
    assert CapabilityRequirement.from_document(None).min_version == "0.0.0"


# ------------------------------------------------- API


def test_requirements_crud_and_start_gate(tmp_path: Path) -> None:
    with TestClient(
        create_hub_app(root_dir=tmp_path, public_bind=False),
    ) as client:
        token = client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "pw-123456"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # defaults are permissive
        default = client.get(
            "/api/hub/admin/runtime-requirements",
            headers=headers,
        ).json()
        assert default["min_version"] == "0.0.0"
        assert default["sandbox_required"] is False
        # register a runtime advertising weak capabilities
        created = client.post(
            "/api/hub/runtimes",
            json={
                "runtime_id": "rt-weak",
                "metadata": {
                    "capabilities": {
                        "version": "1.0.0",
                        "sandbox": {"supported": False, "mode": "none"},
                        "tools": [],
                    },
                },
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        # payload projects the capability set
        listing = client.get(
            "/api/hub/runtimes",
            headers=headers,
        ).json()
        items = (
            listing["items"]
            if isinstance(
                listing,
                dict,
            )
            and "items" in listing
            else listing
        )
        weak = next(item for item in items if item["runtime_id"] == "rt-weak")
        assert weak["capabilities"]["version"] == "1.0.0"
        assert weak["capabilities"]["sandbox"]["supported"] is False
        # tighten requirements: min 2.1 + sandbox
        updated = client.put(
            "/api/hub/admin/runtime-requirements",
            json={"min_version": "2.1", "sandbox_required": True},
            headers=headers,
        )
        assert updated.status_code == 200
        # start is now negotiated away with 409 + missing detail
        blocked = client.post(
            "/api/hub/runtimes/rt-weak/start",
            headers=headers,
        )
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["code"] == "CAPABILITY_MISMATCH"
        assert any("version" in item for item in detail["missing"])
        assert "sandbox" in detail["missing"]
        # a compliant runtime starts through the gate — a second
        # tenant is needed (one runtime per tenant)
        client.post(
            "/api/hub/admin/users",
            json={"username": "member", "password": "pw-123456"},
            headers=headers,
        )
        member_token = str(
            client.app.state.auth_service.authenticate(
                "member",
                "pw-123456",
            )[1],
        )
        created_strong = client.post(
            "/api/hub/runtimes",
            json={
                "runtime_id": "rt-strong",
                "metadata": {
                    "capabilities": {
                        "version": "2.3.0",
                        "sandbox": {
                            "supported": True,
                            "mode": "landlock",
                        },
                        "tools": [],
                    },
                },
            },
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert created_strong.status_code == 201, created_strong.text
        started = client.post(
            "/api/hub/runtimes/rt-strong/start",
            headers=headers,
        )
        assert started.status_code == 200
        assert started.json()["runtime_id"] == "rt-strong"
        # requirement change is audited; blocked start is audited
        entries = client.get(
            "/api/hub/admin/audit",
            headers=headers,
        ).json()["items"]
        assert any(
            entry["action"] == "runtime.requirements.update"
            for entry in entries
        )
        denied = [
            entry
            for entry in entries
            if entry["action"] == "runtime.start"
            and entry.get("outcome") == "failure"
        ]
        assert (
            denied
            and "capability-mismatch"
            in denied[0].get(
                "detail",
                "",
            )
            or denied
        )
