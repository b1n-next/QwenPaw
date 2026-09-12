# -*- coding: utf-8 -*-
"""Unit tests for the hub model catalog store and runtime bootstrap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.app.model_bootstrap import (
    apply_model_bootstrap,
    parse_bootstrap_payload,
)
from qwenpaw.hub.model_catalog import ModelCatalogStore


@pytest.fixture(name="catalog")
def catalog_fixture(tmp_path: Path) -> ModelCatalogStore:
    return ModelCatalogStore(
        tmp_path / "control.db",
        tmp_path / "secrets" / ".model_catalog_key",
    )


class TestModelCatalogStore:
    def test_upsert_and_get_roundtrip(self, catalog: ModelCatalogStore):
        record = catalog.upsert_provider(
            provider_id="corp-gpt",
            name="Corp GPT Gateway",
            base_url="https://llm.corp.internal/v1",
            api_key="sk-secret-1",
            models=["gpt-4o", "gpt-4o-mini"],
            default_model="gpt-4o",
        )
        assert record.provider_id == "corp-gpt"
        assert record.models == ["gpt-4o", "gpt-4o-mini"]
        assert record.enabled is True
        # masked view never carries the plaintext key
        assert not hasattr(record, "api_key")
        assert record.api_key_set is True

    def test_api_key_decrypt_roundtrip(self, catalog: ModelCatalogStore):
        catalog.upsert_provider(
            provider_id="corp",
            name="Corp",
            base_url="https://llm/v1",
            api_key="sk-plain",
        )
        assert catalog.get_api_key("corp") == "sk-plain"

    def test_ciphertext_is_not_plaintext(self, catalog, tmp_path):
        catalog.upsert_provider(
            provider_id="corp",
            name="Corp",
            base_url="https://llm/v1",
            api_key="sk-plain",
        )
        db = (tmp_path / "control.db").read_bytes()
        assert b"sk-plain" not in db

    def test_rotation_keeps_key_when_omitted(
        self,
        catalog: ModelCatalogStore,
    ):
        catalog.upsert_provider(
            provider_id="corp",
            name="Corp",
            base_url="https://llm/v1",
            api_key="sk-old",
        )
        catalog.upsert_provider(
            provider_id="corp",
            name="Corp Renamed",
            base_url="https://llm2/v1",
        )
        assert catalog.get_api_key("corp") == "sk-old"
        updated = catalog.get_provider("corp")
        assert updated.name == "Corp Renamed"

    def test_bootstrap_payload_enabled_only(
        self,
        catalog: ModelCatalogStore,
    ):
        catalog.upsert_provider(
            provider_id="a",
            name="A",
            base_url="https://a/v1",
            api_key="sk-a",
            models=["m1"],
            default_model="m1",
        )
        catalog.upsert_provider(
            provider_id="b",
            name="B",
            base_url="https://b/v1",
            api_key="sk-b",
            enabled=False,
        )
        payload = catalog.bootstrap_payload()
        assert [entry["id"] for entry in payload] == ["a"]
        assert payload[0]["api_key"] == "sk-a"
        assert payload[0]["default_model"] == "m1"

    def test_delete_removes_provider(self, catalog: ModelCatalogStore):
        catalog.upsert_provider(
            provider_id="x",
            name="X",
            base_url="https://x/v1",
        )
        assert catalog.delete_provider("x") is True
        assert catalog.get_provider("x") is None
        assert catalog.delete_provider("x") is False

    def test_invalid_provider_id_rejected(self, catalog):
        with pytest.raises(ValueError):
            catalog.upsert_provider(
                provider_id="Bad Id!",
                name="Bad",
                base_url="https://x/v1",
            )

    def test_key_file_persists_across_instances(self, tmp_path: Path):
        catalog_a = ModelCatalogStore(
            tmp_path / "control.db",
            tmp_path / "secrets" / ".model_catalog_key",
        )
        catalog_a.upsert_provider(
            provider_id="corp",
            name="Corp",
            base_url="https://llm/v1",
            api_key="sk-keep",
        )
        catalog_b = ModelCatalogStore(
            tmp_path / "control.db",
            tmp_path / "secrets" / ".model_catalog_key",
        )
        assert catalog_b.get_api_key("corp") == "sk-keep"


class FakeProviderManager:
    def __init__(self, existing_ids=None, active_model=None):
        self.existing_ids = set(existing_ids or [])
        self.added = []
        self.activations = []
        self.active_model = active_model

    def get_provider(self, provider_id):
        return object() if provider_id in self.existing_ids else None

    async def add_custom_provider(self, provider_info):
        self.added.append(provider_info)

    def get_active_model(self):
        return self.active_model

    async def activate_model(self, provider_id, model_id):
        self.activations.append((provider_id, model_id))


def _payload(entries):
    return json.dumps(entries)


class TestRuntimeBootstrap:
    def test_parse_rejects_garbage(self):
        assert parse_bootstrap_payload(None) == []
        assert parse_bootstrap_payload("") == []
        assert parse_bootstrap_payload("not-json") == []
        assert parse_bootstrap_payload('{"not":"a list"}') == []
        assert parse_bootstrap_payload('[{"id":"x"}]') == []

    async def test_registers_absent_providers(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_MODEL_BOOTSTRAP_JSON",
            _payload(
                [
                    {
                        "id": "corp-gpt",
                        "name": "Corp",
                        "base_url": "https://llm/v1",
                        "api_key": "sk-1",
                        "models": ["gpt-4o"],
                        "default_model": "gpt-4o",
                    },
                ],
            ),
        )
        manager = FakeProviderManager()
        applied = await apply_model_bootstrap(manager)
        assert applied == 1
        assert manager.added[0].id == "corp-gpt"
        assert manager.added[0].api_key == "sk-1"
        # no active model + default available -> activated
        assert manager.activations == [("corp-gpt", "gpt-4o")]

    async def test_existing_provider_left_untouched(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_MODEL_BOOTSTRAP_JSON",
            _payload(
                [
                    {
                        "id": "corp-gpt",
                        "name": "Corp",
                        "base_url": "https://llm/v1",
                        "api_key": "sk-1",
                        "models": ["gpt-4o"],
                    },
                ],
            ),
        )
        manager = FakeProviderManager(existing_ids=["corp-gpt"])
        assert await apply_model_bootstrap(manager) == 0
        assert manager.added == []

    async def test_active_model_never_overridden(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_MODEL_BOOTSTRAP_JSON",
            _payload(
                [
                    {
                        "id": "corp",
                        "name": "Corp",
                        "base_url": "https://llm/v1",
                        "models": ["m1"],
                        "default_model": "m1",
                    },
                ],
            ),
        )
        manager = FakeProviderManager(active_model=object())
        await apply_model_bootstrap(manager)
        assert manager.activations == []

    async def test_default_model_must_be_in_models(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_MODEL_BOOTSTRAP_JSON",
            _payload(
                [
                    {
                        "id": "corp",
                        "name": "Corp",
                        "base_url": "https://llm/v1",
                        "models": ["m1"],
                        "default_model": "m-other",
                    },
                ],
            ),
        )
        manager = FakeProviderManager()
        await apply_model_bootstrap(manager)
        assert manager.activations == []

    async def test_broken_entry_does_not_block_others(self, monkeypatch):
        monkeypatch.setenv(
            "QWENPAW_MODEL_BOOTSTRAP_JSON",
            _payload(
                [
                    {
                        "id": "good",
                        "name": "Good",
                        "base_url": "https://llm/v1",
                        "models": [],
                    },
                    {"no": "id"},
                ],
            ),
        )
        manager = FakeProviderManager()

        class FailingManager(FakeProviderManager):
            async def add_custom_provider(self, provider_info):
                if provider_info.id == "boom":
                    raise RuntimeError("boom")
                self.added.append(provider_info)

        manager = FailingManager()
        applied = await apply_model_bootstrap(manager)
        assert applied == 1
