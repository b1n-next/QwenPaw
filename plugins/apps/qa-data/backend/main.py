# -*- coding: utf-8 -*-
"""问数 (qa-data) — PawApp backend, M1 封装版 (J4 / EP-2-6).

单数据源（QADATA_DATABASE_URL，M1 写死语义）+ schema 内省缓存 +
两把治理工具（search_schema / run_readonly_sql）+ Ask HTTP 路由。
SQL 守卫 fail-closed：非只读、多语句、解析失败一律拒绝。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from guard import GuardError, check_readonly_sql  # noqa: E402
from introspect import SchemaCache  # noqa: E402
from qwenpaw.pawapp import PawApp, get_ctx

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

app = PawApp(name="问数", app_id="qa-data")
router = APIRouter(prefix="/api/qa-data", tags=["qa-data"])

_schema = SchemaCache()

# ── HTTP routes ──────────────────────────────────────────────────────


class AskRequest(BaseModel):
    """One natural-language business question."""

    question: str
    table: str | None = None


@router.get("/tables")
async def list_tables(_ctx=Depends(get_ctx)) -> dict[str, Any]:
    """Tables (+columns) currently cached from the source."""
    return {
        "tables": [
            {"name": t.name, "comment": t.comment, "columns": t.columns}
            for t in _schema.tables()
        ],
    }


@router.post("/refresh")
async def refresh_schema(_ctx=Depends(get_ctx)) -> dict[str, Any]:
    """Force a schema reload (M1 admin-ish action)."""
    try:
        count = _schema.refresh()
    except Exception as exc:  # noqa: BLE001 - surface as 503
        raise HTTPException(
            status_code=503,
            detail=f"schema refresh failed: {exc}",
        ) from exc
    return {"tables": count}


@router.post("/ask")
async def ask(body: AskRequest, ctx=Depends(get_ctx)) -> dict[str, Any]:
    """Answer a business question via the qa-data agent (ctx.chat).

    The agent must call the two governed tools; the answer carries the
    exact SQL it ran plus rows (J4 SQL 透明).
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question required")
    hint = f"（优先在表 {body.table} 范围内检索）" if body.table else ""
    prompt = (
        f"回答业务问题：{question}{hint}。"
        "先调用 search_schema 找相关表列，再用 run_readonly_sql 取数；"
        "最终答复必须包含：所用 SQL 原文（```sql 代码块）与紧凑结果表；"
        "口径不确定时明确说明并引用表/列注释。"
    )
    try:
        answer = await ctx.chat(prompt)
    except Exception as exc:  # noqa: BLE001 - chat backend errors → 502
        raise HTTPException(
            status_code=502,
            detail=f"agent chat failed: {exc}",
        ) from exc
    return {"question": question, "answer": answer}


# ── Governed agent tools ─────────────────────────────────────────────


@app.tool(
    "search_schema",
    description=(
        "按业务问题检索相关数据表与列（关键词打分）。"
        "输入：自然语言查询；返回：表名、注释、列名/类型/注释、相关度。"
        "生成 SQL 前必须先调用本工具。"
    ),
    icon="🔍",
)
def search_schema(query: str) -> str:
    """Agent-facing schema search over the introspected source."""
    hits = _schema.search_schema(query)
    return json.dumps({"tables": hits}, ensure_ascii=False)


@app.tool(
    "run_readonly_sql",
    description=(
        "执行只读 SQL 取数（仅 SELECT/SHOW/DESCRIBE，单语句，"
        "自动注入 LIMIT，最多 200 行）。输入：SQL 字符串。"
        "返回：columns/rows/truncated。任何写操作都会被拒绝。"
    ),
    icon="🛡️",
)
def run_readonly_sql(sql: str, max_rows: int = 200) -> str:
    """Agent-facing guarded execution (fail-closed on policy breach)."""
    try:
        checked = check_readonly_sql(sql, force_limit=max_rows)
    except GuardError as exc:
        return json.dumps(
            {"error": "GUARD_REJECTED", "message": str(exc)},
            ensure_ascii=False,
        )
    cap = min(int(max_rows), 200)
    engine = create_engine(_schema.database_url, future=True)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(checked.sql))
            columns = list(result.keys())
            rows = [
                [None if v is None else str(v) for v in row]
                for row in result.fetchmany(cap + 1)
            ]
    finally:
        engine.dispose()
    truncated = len(rows) > cap
    return json.dumps(
        {
            "sql": checked.sql,
            "limit_applied": checked.limit_applied,
            "columns": columns,
            "rows": rows[:cap],
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


app.include_router(router)
