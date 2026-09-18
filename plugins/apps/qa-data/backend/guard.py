# -*- coding: utf-8 -*-
"""Read-only SQL guard for the qa-data app (J4/EP-2-6).

sqlglot-parsed policy: only single SELECT/SHOW/DESCRIBE statements,
no CTE-writing tricks, and a forced row cap via LIMIT when absent.
Anything unparsable fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

_ALLOWED_ROOTS = (exp.Select, exp.Union)
MAX_ROWS_HARD_CAP = 500

_MULTIPLE = "只允许单条语句（multi-statement 被拒绝）"
_NOT_READONLY = "只允许只读查询：SELECT / SHOW / DESCRIBE"
_NOT_PARSED = "SQL 解析失败（fail-closed 拒绝执行）"


class GuardError(ValueError):
    """Raised when a statement violates the read-only policy."""


@dataclass(frozen=True)
class GuardResult:
    """Normalized statement plus applied guard notes."""

    sql: str
    limit_applied: bool


def check_readonly_sql(sql: str, *, force_limit: int = 200) -> GuardResult:
    """Validate and normalize one read-only statement.

    Raises GuardError on any violation (fail-closed).
    """
    text = (sql or "").strip().rstrip(";")
    if not text:
        raise GuardError(_NOT_PARSED)

    try:
        statements = sqlglot.parse(text, read=None)
    except sqlglot.errors.ParseError as exc:
        raise GuardError(f"{_NOT_PARSED}: {exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise GuardError(_MULTIPLE)

    tree = statements[0]
    if not isinstance(tree, _ALLOWED_ROOTS):
        # SHOW/DESCRIBE arrive as Command nodes on some dialects
        kind = (tree.key or "").lower() if hasattr(tree, "key") else ""
        if kind != "command":
            raise GuardError(_NOT_READONLY)

    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop)):
            raise GuardError(_NOT_READONLY)
        if isinstance(node, exp.Into):
            raise GuardError(_NOT_READONLY)

    limit = min(int(force_limit), MAX_ROWS_HARD_CAP)
    limit_applied = False
    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(limit)
        limit_applied = True
    return GuardResult(sql=tree.sql(), limit_applied=limit_applied)
