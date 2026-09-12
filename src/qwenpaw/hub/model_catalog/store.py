# -*- coding: utf-8 -*-
"""Hub-side centralized model provider catalog (EP-1-1).

Stores admin-maintained provider entries (base_url / models / encrypted
api key) in the hub control database and produces the server-side-only
bootstrap payload injected into tenant runtimes via
``QWENPAW_MODEL_BOOTSTRAP_JSON``. Plaintext keys never leave this
module except through :meth:`ModelCatalogStore.bootstrap_payload` and
:meth:`ModelCatalogStore.get_api_key`, both hub-internal.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_provider_id(provider_id: str) -> str:
    """Validate a catalog provider slug (aligned with runtime ids)."""
    if not _PROVIDER_ID_RE.match(provider_id):
        raise ValueError(
            "provider_id must be a slug: lowercase letters, digits and "
            "dashes, 1-64 chars, starting alphanumeric",
        )
    return provider_id


@dataclass(slots=True)
class ModelProviderRecord:
    """Public (masked) view of one catalog provider entry."""

    provider_id: str
    name: str
    base_url: str
    models: list[str]
    default_model: str | None
    enabled: bool
    api_key_set: bool
    created_at: str
    updated_at: str


class ModelCatalogStore:
    """SQLite-backed model provider catalog with encrypted api keys."""

    def __init__(self, database_path: Path, key_path: Path) -> None:
        self._database_path = Path(database_path)
        self._key_path = Path(key_path)
        self._fernet = Fernet(self._load_or_create_key())
        self._init_tables()

    # -- schema ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _load_or_create_key(self) -> bytes:
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self._key_path.write_bytes(key)
        try:
            self._key_path.chmod(0o600)
        except OSError:
            pass
        return key

    def _init_tables(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_providers (
                  provider_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  base_url TEXT NOT NULL,
                  api_key_ciphertext TEXT NOT NULL,
                  models TEXT NOT NULL,
                  default_model TEXT,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenant_model_grants (
                  tenant_id TEXT NOT NULL,
                  provider_id TEXT NOT NULL,
                  granted_models TEXT,
                  PRIMARY KEY (tenant_id, provider_id)
                );
                """,
            )

    # -- CRUD -----------------------------------------------------------

    def upsert_provider(
        self,
        *,
        provider_id: str,
        name: str,
        base_url: str,
        api_key: str | None = None,
        models: list[str] | None = None,
        default_model: str | None = None,
        enabled: bool = True,
    ) -> ModelProviderRecord:
        """Insert or update one provider.

        ``api_key=None`` keeps the previously stored key (rotation is
        opt-in so PATCH payloads never need to echo secrets).
        """
        provider_id = validate_provider_id(provider_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM model_providers "
                "WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if api_key is None and row is not None:
                ciphertext = row["api_key_ciphertext"]
            else:
                ciphertext = self._fernet.encrypt(
                    (api_key or "").encode("utf-8"),
                ).decode("ascii")
            connection.execute(
                """
                INSERT INTO model_providers (
                  provider_id, name, base_url, api_key_ciphertext,
                  models, default_model, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                  name = excluded.name,
                  base_url = excluded.base_url,
                  api_key_ciphertext = excluded.api_key_ciphertext,
                  models = excluded.models,
                  default_model = excluded.default_model,
                  enabled = excluded.enabled,
                  updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    name,
                    base_url,
                    ciphertext,
                    json.dumps(models or []),
                    default_model,
                    int(enabled),
                    now,
                    now,
                ),
            )
        record = self.get_provider(provider_id)
        assert record is not None
        return record

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ModelProviderRecord:
        return ModelProviderRecord(
            provider_id=row["provider_id"],
            name=row["name"],
            base_url=row["base_url"],
            models=json.loads(row["models"]),
            default_model=row["default_model"],
            enabled=bool(row["enabled"]),
            api_key_set=bool(row["api_key_ciphertext"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_providers(
        self,
        include_disabled: bool = True,
    ) -> list[ModelProviderRecord]:
        query = "SELECT * FROM model_providers"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY provider_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_provider(
        self,
        provider_id: str,
    ) -> ModelProviderRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def delete_provider(self, provider_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM model_providers WHERE provider_id = ?",
                (provider_id,),
            )
            connection.execute(
                "DELETE FROM tenant_model_grants WHERE provider_id = ?",
                (provider_id,),
            )
        return cursor.rowcount > 0

    # -- server-side secrets ---------------------------------------------

    def get_api_key(self, provider_id: str) -> str:
        """Decrypt the api key (hub-internal use only)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM model_providers "
                "WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        if row is None or not row["api_key_ciphertext"]:
            raise KeyError(provider_id)
        return self._fernet.decrypt(
            row["api_key_ciphertext"].encode("ascii"),
        ).decode("utf-8")

    def bootstrap_payload(self) -> list[dict[str, Any]]:
        """Build the runtime bootstrap payload (enabled providers only).

        Contains decrypted api keys; must only be serialized into the
        hub->runtime environment, never into an API response or log.
        """
        payload: list[dict[str, Any]] = []
        for record in self.list_providers(include_disabled=False):
            try:
                api_key = self.get_api_key(record.provider_id)
            except KeyError:
                api_key = ""
            payload.append(
                {
                    "id": record.provider_id,
                    "name": record.name,
                    "base_url": record.base_url,
                    "api_key": api_key,
                    "models": record.models,
                    "default_model": record.default_model,
                },
            )
        return payload
