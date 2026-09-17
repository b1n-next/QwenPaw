# -*- coding: utf-8 -*-
"""Graph schema: nodes, routed edges, and DAG validation (EP-2-17)."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class GraphNode(BaseModel):
    """One executable step; semantics resolved by the executor handler."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=120)
    params: Dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Directed edge with optional route matching.

    A node's result carries a ``route`` (default ``"default"``); the
    outgoing edges activated for that result are exactly the edges whose
    ``when`` equals the route. Edges without ``when`` match only the
    default route — that is how human-gate approve/deny branches are
    expressed (EP-2-15 semantics at graph level).
    """

    model_config = ConfigDict(extra="forbid")

    from_: str = Field(alias="from")
    to: str
    when: str = Field(default="default")

    @field_validator("when")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GraphSchema(BaseModel):
    """A validated executable DAG."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    entry: str
    nodes: List[GraphNode] = Field(min_length=1, max_length=200)
    edges: List[GraphEdge] = Field(default_factory=list, max_length=400)

    @field_validator("entry")
    @classmethod
    def _entry_exists_later(cls, value: str) -> str:
        # Real check happens in the model validator (needs nodes).
        return value

    @model_validator(mode="after")
    def _validate_graph(self) -> "GraphSchema":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Node ids must be unique")
        known = set(node_ids)
        if self.entry not in known:
            raise ValueError(f"Entry node '{self.entry}' not in nodes")
        for edge in self.edges:
            if edge.from_ not in known:
                raise ValueError(f"Edge source '{edge.from_}' not in nodes")
            if edge.to not in known:
                raise ValueError(f"Edge target '{edge.to}' not in nodes")
            if edge.from_ == edge.to:
                raise ValueError(f"Self-loop on node '{edge.from_}'")
        self._ensure_acyclic()
        return self

    def _ensure_acyclic(self) -> None:
        """Kahn's algorithm — reject any cycle."""
        indegree = {node.id: 0 for node in self.nodes}
        adjacency: Dict[str, List[str]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            indegree[edge.to] += 1
            adjacency[edge.from_].append(edge.to)
        queue = [nid for nid, deg in indegree.items() if deg == 0]
        visited = 0
        while queue:
            current = queue.pop()
            visited += 1
            for nxt in adjacency[current]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(self.nodes):
            raise ValueError("Graph must be acyclic (cycle detected)")

    # NOTE: multiple edges sharing (source, route) are legal fan-out —
    # every activated edge fires, so parallel branches express naturally.
    # Distinct routes on the same source express gate branching.

    # -------------------------------------------------------- accessors

    def node(self, node_id: str) -> GraphNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def outgoing(self, node_id: str) -> List[GraphEdge]:
        return [edge for edge in self.edges if edge.from_ == node_id]


__all__ = ["GraphEdge", "GraphNode", "GraphSchema"]
