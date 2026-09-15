# -*- coding: utf-8 -*-
"""EP-2-16: plugin-registered harness providers.

Third-party harnesses join the provider catalog without forking: the
plugin API lands a catalog item + adapter factory in the dynamic
registry, builtin ids stay reserved, and the generic config surface
(binary/env/args) drives adapter recreation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# pylint: disable=protected-access

import pytest

from qwenpaw.harnesses.base import HarnessAdapter
from qwenpaw.harnesses.events import (
    HarnessCapabilities,
    HarnessEventKind,
    HarnessProvider,
)
from qwenpaw.harnesses.registry import (
    ProviderCatalogItem,
    adapter_config_key,
    create_adapter,
    get_provider,
    list_provider_items,
    register_plugin_provider,
    unregister_plugin_provider,
)


def _demo_factory(state_dir: Path, settings: dict) -> "_DemoAdapter":
    """Shared registration factory (two-arg signature by contract)."""
    return _DemoAdapter(state_dir, settings)


class _DemoAdapter(HarnessAdapter):
    """Minimal echo-style adapter."""

    def __init__(self, state_dir: Path, settings: dict) -> None:
        self.state_dir = state_dir
        self.settings = settings

    async def status(self) -> HarnessProvider:
        return HarnessProvider(
            id="demo",
            name="Demo",
            available=True,
            installed=True,
            authenticated=True,
        )

    async def start_login(self, device_code: bool = False) -> dict:
        return {}

    async def logout(self) -> None:
        return None

    def run_turn(self, **_kwargs):
        async def _stream():
            return
            yield  # pragma: no cover - empty async generator

        return _stream()

    async def stop(self) -> None:
        return None


@pytest.fixture(name="demo_registered")
def _demo_registered():
    register_plugin_provider(
        ProviderCatalogItem(
            id="demo",
            name="Demo Harness",
            coming_soon=False,
            capabilities=HarnessCapabilities(),
        ),
        _demo_factory,
    )
    yield "demo"
    unregister_plugin_provider("demo")


# --------------------------------------------------------------- catalog


def test_plugin_provider_joins_catalog(demo_registered) -> None:
    del demo_registered  # fixture registers the provider
    ids = [item.id for item in list_provider_items()]
    assert ids[:3] == ["codex", "claude", "qoder"]
    assert "demo" in ids
    assert get_provider("demo").name == "Demo Harness"


def test_builtin_ids_are_reserved(demo_registered) -> None:
    del demo_registered  # fixture registers the provider
    with pytest.raises(ValueError, match="reserved"):
        register_plugin_provider(
            ProviderCatalogItem(
                id="codex",
                name="Impostor",
                coming_soon=False,
                capabilities=HarnessCapabilities(),
            ),
            _demo_factory,
        )


def test_reregistration_is_idempotent(demo_registered) -> None:
    del demo_registered  # fixture registers the provider
    # hot reload: same id replaces cleanly
    register_plugin_provider(
        ProviderCatalogItem(
            id="demo",
            name="Demo Harness v2",
            coming_soon=False,
            capabilities=HarnessCapabilities(),
        ),
        _demo_factory,
    )
    assert get_provider("demo").name == "Demo Harness v2"


# ----------------------------------------------------------- adaptation


def test_create_adapter_uses_plugin_factory(demo_registered, tmp_path):
    del demo_registered  # fixture registers the provider
    adapter = create_adapter(
        "demo",
        tmp_path,
        {"binary": "/usr/bin/demo", "env": {"K": "V"}, "args": ["-x"]},
    )
    assert isinstance(adapter, _DemoAdapter)
    assert adapter.settings["binary"] == "/usr/bin/demo"


def test_config_key_covers_generic_fields(demo_registered) -> None:
    del demo_registered  # fixture registers the provider
    base = adapter_config_key(
        "demo",
        {"binary": "b", "env": {"A": "1"}, "args": ["-x"]},
    )
    assert base == ("b", (("A", "1"),), ("-x",))
    changed = adapter_config_key(
        "demo",
        {"binary": "b", "env": {"A": "2"}, "args": ["-x"]},
    )
    assert changed != base  # env change forces adapter recreation
    same = adapter_config_key(
        "demo",
        {"binary": "b", "env": {"A": "1"}, "args": ["-x"], "extra": 1},
    )
    assert same == base  # undeclared fields do not


def test_unregister_removes_provider(demo_registered) -> None:
    del demo_registered  # fixture registers the provider
    unregister_plugin_provider("demo")
    ids = [item.id for item in list_provider_items()]
    assert "demo" not in ids
    with pytest.raises(ValueError):
        create_adapter("demo", Path("."), {})


# --------------------------------------------------------- plugin API


def test_plugin_api_registers_harness() -> None:
    from qwenpaw.plugins.api import PluginApi

    class _ApiAdapter(_DemoAdapter):
        async def status(self) -> HarnessProvider:
            return HarnessProvider(
                id="apidemo",
                name="API Demo",
                available=True,
            )

    api = PluginApi.__new__(PluginApi)  # bypass heavy construction
    api.plugin_id = "test-plugin"
    api._registry = None
    api.manifest = {}

    api.register_harness_provider(
        provider_id="apidemo",
        adapter_class=_ApiAdapter,
        name="API Demo Harness",
    )
    try:
        assert get_provider("apidemo").name == "API Demo Harness"
        adapter = create_adapter("apidemo", Path("."), {"binary": "x"})
        assert isinstance(adapter, _ApiAdapter)
    finally:
        unregister_plugin_provider("apidemo")


# ------------------------------------------------------- echo example


def test_echo_example_adapter_streams() -> None:
    """The shipped example is importable and streams echo events."""
    import importlib.util

    example = (
        Path(__file__).resolve().parents[3]
        / "examples"
        / "custom_harness"
        / "echo_plugin.py"
    )
    spec = importlib.util.spec_from_file_location("echo_plugin", example)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    adapter = module.EchoAdapter(
        state_dir=Path("."),
        settings={"binary": "echo", "env": {}, "args": []},
    )

    async def _run():
        status = await adapter.status()
        assert status.id == "echo"
        assert status.installed and status.authenticated
        events = []
        stream = adapter.run_turn(
            session_id="s",
            prompt="hello",
            cwd=Path("."),
            settings={},
        )
        async for event in stream:
            events.append(event)
        return events

    events = asyncio.run(_run())
    assert events[0].kind == HarnessEventKind.TEXT_DELTA
    assert events[0].text == "echo: hello"
    assert events[-1].kind == HarnessEventKind.COMPLETED
