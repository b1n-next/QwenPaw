# -*- coding: utf-8 -*-
"""Organization policy baseline catalog for the Hub (EP-2-13).

Admins maintain one organization-wide governance baseline on the hub;
personal runtimes receive it through the provisioner environment
(``QWENPAW_POLICY_BASELINE_JSON``, content + sha256) exactly like the
model catalog bootstrap. On the runtime side the baseline is applied as
a ``hub_rules`` layer that outranks builtin and user rules, so a local
policy.yaml edit can never downgrade what the organization mandated.
"""

from ._store import PolicyCatalogStore, canonical_rule_digest

__all__ = ["PolicyCatalogStore", "canonical_rule_digest"]
