# -*- coding: utf-8 -*-
"""Mobile H5 approval page (Phase 3, G-P14 closing slice).

One dependency-free HTML page served by the runtime: list pending
approvals and resolve them (approve / deny) from a phone. It talks
to the existing /api/approval endpoints with the caller's bearer
token, so no new auth surface is introduced.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["mobile"])

_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a">
<title>QwenPaw 审批</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      "PingFang SC", sans-serif;
    background: #0f172a; color: #e2e8f0;
    min-height: 100vh;
    padding: 16px 14px
      calc(24px + env(safe-area-inset-bottom));
  }
  header { display: flex; align-items: center;
           gap: 10px; margin-bottom: 14px; }
  header h1 { font-size: 18px; font-weight: 600; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; }
  .dot.off { background: #64748b; }
  .card {
    background: #1e293b; border-radius: 14px; padding: 14px;
    margin-bottom: 12px; border: 1px solid #334155;
  }
  .card .agent { font-size: 13px; color: #38bdf8; margin-bottom: 6px;
                 word-break: break-all; }
  .card .summary { font-size: 15px; line-height: 1.5;
                   white-space: pre-wrap; word-break: break-word; }
  .card .meta { font-size: 12px; color: #94a3b8; margin-top: 8px; }
  .actions { display: flex; gap: 10px; margin-top: 12px; }
  button {
    flex: 1; border: none; border-radius: 10px; padding: 13px 0;
    font-size: 15px; font-weight: 600;
  }
  .approve { background: #16a34a; color: #fff; }
  .deny { background: #dc2626; color: #fff; }
  button:disabled { opacity: .45; }
  .empty { text-align: center; color: #64748b;
           padding: 48px 0; font-size: 14px; }
  .login input {
    width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #334155;
    background: #0f172a; color: #e2e8f0; font-size: 14px; margin-bottom: 10px;
  }
  .login button { background: #2563eb; color: #fff; }
  .error { color: #f87171; font-size: 13px; margin: 10px 0;
           word-break: break-all; }
  .refresh { color: #38bdf8; font-size: 13px;
             margin-left: auto; cursor: pointer; }
</style>
</head>
<body>
<header>
  <div class="dot off" id="statusDot"></div>
  <h1>待审批</h1>
  <span class="refresh" onclick="load()">刷新</span>
</header>
<div id="error" class="error"></div>
<div id="root"></div>
<script>
"use strict";
const API = "";
let token = localStorage.getItem("qwenpaw_mobile_token") || "";

function headers() {
  return {
    "Authorization": "Bearer " + token,
    "Content-Type": "application/json",
  };
}

async function call(path, options) {
  const response = await fetch(API + path, options);
  if (response.status === 401) {
    localStorage.removeItem("qwenpaw_mobile_token");
    token = "";
    throw new Error("登录已过期，请重新输入 Token");
  }
  if (!response.ok) {
    let detail = response.status + "";
    try { detail = JSON.stringify(await response.json()); }
    catch (e) { /* noop */ }
    throw new Error(detail);
  }
  return response.json();
}

function renderLogin(message) {
  document.getElementById("statusDot").className = "dot off";
  document.getElementById("root").innerHTML = `
    <div class="card login">
      <input id="tokenInput" type="password"
             placeholder="访问 Token（Bearer）" autocomplete="off">
      <button onclick="saveToken()">进入</button>
    </div>`;
  if (message) document.getElementById("error").textContent = message;
}

function saveToken() {
  token = document.getElementById("tokenInput").value.trim();
  localStorage.setItem("qwenpaw_mobile_token", token);
  load();
}

function summaryText(item) {
  const summary = item.summary || {};
  return summary.tool_name
    ? (summary.tool_name + "\\n" + (summary.summary || ""))
    : (summary.summary || JSON.stringify(summary, null, 2));
}

function cardHtml(item) {
  const esc = (s) => String(s).replace(/&/g, "&amp;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const rid = esc(item.request_id);
  const sid = esc(item.root_session_id || item.session_id || "");
  return `<div class="card" data-rid="${rid}" data-sid="${sid}">
    <div class="agent">${esc(item.owner_agent_id || "agent")}</div>
    <div class="summary">${esc(summaryText(item))}</div>
    <div class="meta">${rid.slice(0, 10)}…</div>
    <div class="actions">
      <button class="approve" onclick="resolve('approve', this)">批准</button>
      <button class="deny" onclick="resolve('deny', this)">驳回</button>
    </div>
  </div>`;
}

async function load() {
  if (!token) { renderLogin(""); return; }
  document.getElementById("error").textContent = "";
  try {
    const data = await call("/api/approval/list", { headers: headers() });
    const items = data.pending_approvals || [];
    document.getElementById("statusDot").className = "dot";
    document.getElementById("root").innerHTML = items.length
      ? items.map(cardHtml).join("")
      : '<div class="empty">暂无待审批事项</div>';
  } catch (err) {
    renderLogin("");
    document.getElementById("error").textContent = err.message;
  }
}

async function resolve(decision, button) {
  const card = button.closest(".card");
  const rid = card.dataset.rid;
  const sid = card.dataset.sid;
  const buttons = card.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await call("/api/approval/" + decision, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ request_id: rid, session_id: sid }),
    });
    card.remove();
    if (!document.querySelector(".card")) {
      document.getElementById("root").innerHTML =
        '<div class="empty">暂无待审批事项</div>';
    }
  } catch (err) {
    document.getElementById("error").textContent = err.message;
    buttons.forEach((b) => { b.disabled = false; });
  }
}

load();
setInterval(load, 10000);
</script>
</body>
</html>
"""


@router.get("/mobile/approvals", response_class=HTMLResponse)
async def mobile_approvals_page() -> HTMLResponse:
    """Serve the dependency-free mobile approval page."""
    return HTMLResponse(_PAGE)


__all__ = ["router"]
