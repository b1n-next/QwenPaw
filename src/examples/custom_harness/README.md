# Custom Harness 示例（EP-2-16）

不 fork QwenPaw 源码，把一个第三方 agent harness 接入 provider 列表。

## 结构

```
custom_harness/
├── plugin.json      # 插件清单（type: provider）
├── echo_plugin.py   # EchoAdapter + register(api) 入口
└── README.md
```

## 工作原理

1. `EchoAdapter` 继承 `qwenpaw.harnesses.base.HarnessAdapter`，约定构造签名
   `(state_dir: Path, settings: dict)`。
2. `register(api)` 调用 `api.register_harness_provider(...)` —— 该扩展点把
   catalog 条目与 adapter 工厂写进 harness registry（`harnesses/registry.py`
   的插件表），builtin id（codex/claude/qoder）保留不可覆盖。
3. 之后：

   - provider 列表（harness runtime `providers()`）出现 "Echo Harness"；
   - `create_adapter("echo", state_dir, settings)` 走插件工厂；
   - `settings` 里的 `binary / env / args` 变化会触发 adapter 重建
     （`adapter_config_key` 对插件字段做泛化比较）。

## 真实 CLI harness 迁移指引

把 `run_turn` 的 echo 逻辑换成子进程驱动：

```python
proc = await asyncio.create_subprocess_exec(
    self._binary, *self._args, "--prompt", prompt,
    cwd=cwd, env={**os.environ, **self._env},
    stdout=asyncio.subprocess.PIPE,
)
async for raw in proc.stdout:
    yield HarnessEvent(kind=HarnessEventKind.TEXT_DELTA, text=raw.decode())
```

并将 `status()` 的 `installed/authenticated` 改为探测 CLI 真实状态。

## 安装

将本目录放入 QwenPaw 插件目录（或通过插件加载器配置指向），重启后
provider 列表即出现 "Echo Harness"。

## 验收（DoD）

- [x] 不改 QwenPaw 源码接入示例 harness
- [x] 示例出现在 provider 列表（`list_provider_items()` / runtime providers）
- [x] 配置面泛化：binary / env / args 经 settings 传入并参与重建判定
