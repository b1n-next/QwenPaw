# -*- coding: utf-8 -*-
"""Validated request models for QwenPaw Hub APIs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import HubConfig
from .user_profile import HubUserProfile


class RuntimeCreateBody(BaseModel):
    """Request body for a new managed runtime."""

    model_config = ConfigDict(extra="forbid")

    runtime_id: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    auto_start: bool = False


class RuntimeRequirementsBody(BaseModel):
    """Admin-managed scheduling requirements (G2)."""

    model_config = ConfigDict(extra="forbid")

    min_version: str = Field(default="0.0.0", max_length=16)
    sandbox_required: bool = False
    tools_required: list[str] = Field(default_factory=list)


class DockerImagePullBody(BaseModel):
    """Request an asynchronous Docker image pull."""

    reference: str = Field(min_length=1, max_length=512)


class CredentialsBody(BaseModel):
    """Login or bootstrap registration credentials."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=1024)
    invite_code: str | None = Field(default=None, max_length=128)


class AdminUserCreateBody(CredentialsBody):
    """Administrator request for a managed account."""

    role: str = "user"


class AdminUserPatchBody(BaseModel):
    """Administrator changes that invalidate existing user tokens."""

    role: str | None = None
    disabled: bool | None = None
    profile: HubUserProfile | None = None


class PasswordChangeBody(BaseModel):
    """Authenticated password replacement request."""

    new_password: str = Field(min_length=8, max_length=1024)


class HubSettingsBody(BaseModel):
    """Atomic administrator update for the complete Hub configuration."""

    revision: int = Field(ge=1)
    config: HubConfig


class CredentialBody(BaseModel):
    """Tenant-scoped credential write without a plaintext read endpoint."""

    scope: str = "tenant"
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=65536)
