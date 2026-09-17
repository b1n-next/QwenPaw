# -*- coding: utf-8 -*-
"""Directed-acyclic graph execution engine (EP-2-17).

Layers:
    schema.py     — pydantic graph schema (nodes/edges/entry, DAG check)
    state_store.py— SQLite run + per-node checkpoints
    executor.py   — topological async executor with branch routing,
                    suspension (human gates) and resume-from-checkpoint

Node semantics stay pluggable: handlers are registered per node ``kind``
(EP-2-18 wires the canvas node types; EP-2-19 templates), so the engine
itself is provable with echo handlers today.
"""
