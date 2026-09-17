# -*- coding: utf-8 -*-
"""A2A 1.0 server and client (EP-2-21).

Server side exposes the standard agent-card discovery plus a
JSON-RPC message endpoint (send / stream / tasks/get) over the
existing inter-agent chat task API. Client side is the core module
external plugins (cloudpaw et al.) reuse instead of shipping their
own protocol code.
"""

from .client import A2AClient, discover_agent_card
from .server import agent_card, api_router, well_known_router

__all__ = [
    "A2AClient",
    "discover_agent_card",
    "agent_card",
    "api_router",
    "well_known_router",
]
