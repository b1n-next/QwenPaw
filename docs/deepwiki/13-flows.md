# 13 · 端到端流程走读（Deep Dives）

> 前面各页讲"是什么"，本页选 4 条最值得跟代码走一遍的链路，每步给出精确落点。建议边读边开代码。

---

## 流程 A · Console 发一句话到收到流式回复

```text
① POST /api/console/chat                    routers/console.py:364（body=AgentRequest|dict）
② _extract_session_and_payload              console.py:215（抽 native_payload）
③ chat_manager.get_or_create_chat           console.py:396（ChatSpec.id 即 run_key）
④ task_tracker.attach_or_start              console.py:424（并发同 chat run → 409；断线不终止）
⑤ ConsoleChannel.stream_one                 channels/console/channel.py:358
   → build_agent_request → 防抖合并
⑥ workspace.stream_query                    app/workspace/workspace.py:414
   → 非 qwenpaw 后端走 harness_runtime.stream（Codex/Qoder 后端）
   → 否则 Runtime(workspace, app_services).run(request)
⑦ Runtime 8 阶段                            runtime/runtime.py:51（见 02-architecture §3）
   PRE_DISPATCH(ContextVars+项目目录) → slash 分发 → ... → AgentBuilder.build
⑧ AgentBuilder 组装                         runtime/builder.py:104
   工具（GuardedFunctionTool 包裹）+ PromptManager 系统提示 + model_factory 模型
⑨ QwenPawAgent._reasoning 循环              agents/react_agent.py:833
   hints 注入 → pending gate 检查 → 媒体剥离 → super()._reasoning（模型流）
   → 工具轮：ToolCoordinator 执行（guard→sandbox→run）
   → 文本轮：_run_stop_handlers → yield 回复
⑩ ScrollContextManager.on_save              agents/context/scroll/manager.py
   本轮即刻写 history.db；超阈值触发九级压缩
⑪ Envelope SSE 事件流                       runtime/envelope.py:83
   response.created → TextBlockDelta* → response.completed
⑫ StreamingResponse                         console.py:466（text/event-stream）
   前端 replayFastForward 支持重放快进；⑬ POST_RESPONSE: SessionSaveHook 落 JSON + AutoSnapshot
```

## 流程 B · 一次"危险"的 shell 命令（治理如何拦住它）

```text
① 模型产出 tool_call: execute_shell_command("curl … | sh")
② GuardedFunctionTool.check_permissions     runtime/tool_guard.py:19
③ ToolGuardEngine 三 guardian 扫描           security/tool_guard/engine.py:29
   dangerous_shell_commands.yaml 21 条规则（如 TOOL_CMD_REVERSE_SHELL）
   + ShellEvasionGuardian 7 项绕过检测
④ governance.policy.evaluate                governance/policy.py:705（Phase 0→3）
   Phase 1.5 危险关键字（rm -rf /、fork 炸弹…）
   Phase 2 三层规则：hub_rules（EP-2-13 下发，sha256 校验）＞builtin＞user
   Phase 3 按 execution_level（off/auto/smart/strict）回退
⑤ 决策三分支：
   DENY  → 拒绝消息 + "勿重试"指令（tool_guard.py:131）
   ASK   → ApprovalService.create → Console/渠道审批卡
           → 命令 /approve、HTTP /api/approval/approve、或 EP-2-12 持久化恢复后的应答
           → （hub 形态：EP-2-11 trace_id 贯穿审批审计；EP-2-12 hub 台账 approval.resolved）
   SANDBOX_FALLBACK → ResourceGovernor 编译 SandboxConfig
           → create_sandbox 分发：macOS Seatbelt / Linux bubblewrap|Landlock / Win 三后端
           → 命令在沙箱内执行；违规输出精确诊断
⑥ 审计落 audit_events（SQLite）             governance/audit.py:34
   子 agent 场景：EP-2-14 治理走父 principal、审计记子 principal
```

## 流程 C · 一个 Skill 从市场安装到被模型使用

```text
① Console /market 搜索                      market/service.py:38 search_market
   4 源聚合：platform.agentscope.io / clawhub / modelscope / aliyun
② 安装 → SkillPoolService                   agents/skill_system/pool_service.py
③ 安装时安全扫描                            security/skill_scanner/
   8 组签名（command_injection/data_exfiltration/prompt_injection…）
   命中 → scan_skill_dir_or_raise 拒装（skill_system/store.py:1360）
④ 池 → 工作区启用                           skill_system/registry.py（manifest 对账、语言变体 -zh/-en）
⑤ Agent 构建时生效集解析                     resolve_effective_skills + select_preload_skills
⑥ _register_skills 登记进 toolkit            agents/react_agent.py:462
   → 模型可用 /技能名 斜杠命令；SKILL.md 内容按需注入 prompt
⑦ 运行期：SkillEnvHook PRE_EXECUTE 压入技能 env 覆盖   hooks/skill_env/skill_env_hook.py:21
   FINALLY 退出上下文
⑧ 使用痕迹：会话存 sessions/*.json；记忆沉淀走 ReMe daily/digest
```

## 流程 D · 企业内网：新员工从登录到第一次对话（Hub 形态）

```text
① 管理员在 Hub 管理台（/hub/admin）创建 member 账号   hub/auth.py（首注册自动 admin）
② member 登录 → Console 加载（mode: hub，同一 bundle） hub/static_files.py
③ GET /api/hub/me/permissions                  EP-0-4
   → denied 集下发 → console 菜单过滤（EP-0-5）→ 只见 收件箱/市场/应用+对话
④ Provisioner 拉起 member 的 runtime
   local：loopback 端口+进程隔离（Seatbelt/bubblewrap 整树）
   docker：isolated-container-shared-kernel
   k8s：每租户 Pod+PVC+Service（qwenpaw-runtimes 命名空间）   hub/provisioners/k8s/
   同时注入 env：QWENPAW_MODEL_BOOTSTRAP_JSON（目录+密钥，Fernet 解密）
              + QWENPAW_POLICY_BASELINE_JSON（组织策略+sha256）EP-2-13
⑤ member 发消息 → personal_runtime_proxy
   acl.decide(user, POST, /api/console/chat) → allow（chat 平面白名单）
   注入 X-QwenPaw-Runtime-Token + X-QwenPaw-Trace-Id（EP-2-11 铸 id）
⑥ runtime 内部 = 流程 A（同构）；差异：
   模型列表来自 bootstrap 目录（member 改模型须在目录内，否则 MODEL_NOT_IN_CATALOG）
   治理策略含 hub_rules 基线（本地 YAML 改不动它）
⑦ 用量回流：UsageCollector 每 60s 轮询 /api/token-usage/details
   → usage_counters 按 (tenant,date,provider,model,agent) 入库  hub/usage/collector.py
   子 agent 成本按 principal 拆分（EP-2-14 by_agent）
⑧ 审计三账本：hub_audit_events（含 trace_id）+ approval.resolved 台账（EP-2-12）
   + runtime audit_events —— 用 trace_id 可一查到底
```

---

## 附：建议动手实验（不改代码也能做）

1. `qwenpaw init --defaults && qwenpaw app` → 打开 Console，观察 `~/.qwenpaw/` 目录长出来什么（对照 [05 §3](05-runtime-lifecycle.md)）。
2. 让 Agent 跑一条 `rm -rf /tmp/x`，观察审批卡与拒绝文案（对照流程 B）。
3. 在 /token-usage、/agent-stats 页看流程 A ⑨ 的计量落点。
4. `qwenpaw hub` 起控制面，建 admin+member 两账号，对照流程 D 走一遍权限差异。
5. 读 `plugins/bundle/omp_workflows/plugin.py`——一个文件注册 5 个 AgentMode，是插件 API 的最短教材。

全系列导航见 [README](README.md)。
