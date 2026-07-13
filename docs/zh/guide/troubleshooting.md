# 故障排除

下面是你最可能遇到的几类故障，每类都给出确切修法。如果遇到下面没列出来的问题，欢迎[提 issue](https://github.com/jeffkit/agentproc/issues)。

## 快速决策树

```
错误信息里说的是什么？
│
├─ "GitHub rate-limited the hub fetch"
│   → 看：Hub 拉取失败 / 限流
│
├─ "profile '<name>' not found in hub"
│   → 看：profile 名字写错
│
├─ "[agentproc runner] spawn error: spawn X ENOENT"
│   → 看：spawn ENOENT
│
├─ stderr 上出现 "{"type":"error"}..."
│   → 是被包装的 agent 自己报错。看：被包装 CLI 的 `{"type":"error"}`
│
├─ 卡住 / 无输出
│   → 看：agent 跑了但没返回内容
│
└─ 退出码 124
    → 看：超时
```

---

## Hub 拉取失败 / 限流

### 症状

```
fetching profile 'claude-code' from jeffkit/agentproc:main...
error: GitHub rate-limited the hub fetch (HTTP 403)

GitHub limits anonymous hub fetches to ~60/hour. ...
```

### 原因

CLI 从 `github.com/jeffkit/agentproc` 拉 hub profile。匿名 GitHub API 请求每个 IP 每小时 ~60 次。CI runner、办公室共享 NAT、或者只是连着跑几次 `hub list` / `hub show` / `hub run` 就可能用光。

### 修法（从快到慢）

1. **等一会。** 限流每小时重置。已经缓存的 profile（`~/.agentproc/cache/hub/<name>/`，24 小时 TTL）不重新拉取，仍可使用。
2. **设置 token。** 鉴权后可提到每小时 5,000 次：
   ```bash
   export GITHUB_TOKEN=$(gh auth token)   # 如果你装了 GitHub CLI
   # 或把 GITHUB_TOKEN 设为任意 personal access token（公开仓库不需要 scope）
   ```
3. **完全绕开网络。** 用本地仓库：
   ```bash
   git clone https://github.com/jeffkit/agentproc
   cd agentproc
   agentproc --profile ./hub/<name>/profile.yaml --prompt "hi"
   ```

---

## profile 名字写错

### 症状

```
error: profile 'claude-codex' not found in hub

Did you mean `claude-code`?

Available profiles:
  - claude-code
  - codex
  - echo-agent
  ...
```

### 原因

profile 名字拼错了。CLI 拉了 hub 目录树、没找到匹配的 `hub/<name>/` 目录，并给出最接近的建议。

### 修法

用建议的名字，或者列出可选项：

```bash
agentproc hub list
```

---

## spawn ENOENT

### 症状

```
[agentproc runner] spawn error: spawn python3 ENOENT
[agentproc runner] hint: <下面某一条>
{"type":"error","message":"failed to start agent: ..."}
```

CLI 会按根因给出针对性提示，下面是各情况的背景。

### 根因 1：`profile.cwd does not exist: <path>`

你传了 `--cwd /some/path`（或 profile 设了 `cwd:`），但这个目录不存在。

**修法：** 让 `--cwd` 指向真实存在的目录：
```bash
agentproc hub run claude-code -p "hi" --cwd /actual/path/to/your/project
```

### 根因 2：`'python3' not found on PATH`

bridge 通过 Node 的 `child_process.spawn` 启动 agent，会继承父进程的 `PATH`。如果 CLI 是从 PATH 不包含解释器的环境启动的（systemd、cron、GUI 启动器、某些 IDE 常见），即使你 shell 里 `python3` 能用，spawn 也会失败。

**修法：** 二选一：
- 让 PATH 包含解释器（软链或写绝对路径）。
- 改用 Node bridge 脚本（`command: node`，`args: ["{{PROFILE_DIR}}/bridge.js"]`）——Node 一定能找到（CLI 本身就跑在 Node 上）。

### 根因 3：`'claude' not found on PATH`（被包装的 CLI 没装）

比如你跑了 `hub run claude-code` 但从来没装 `claude` CLI。

**修法：** 按 profile README 的指引装被包装的 CLI：
```bash
npm install -g @anthropic-ai/claude-code   # claude-code 用
npm install -g @openai/codex                # codex 用
```

用 `agentproc hub show <name>` 查看每个 profile 的安装命令。

### 根因 4：`argument file not found: ./bridge.py`

你在跑一个旧版 hub profile（或手改过的）——`args` 里用了裸的 `./bridge.py` 相对路径，但 agent 的 `cwd` 里没这个文件。

**修法：** 重新安装或刷新 profile（新版 profile 在 `args` 里用 `{{PROFILE_DIR}}/bridge.py`，永远能正确解析）：
```bash
agentproc hub install claude-code --refresh
```

---

## 被包装 CLI 的 `{"type":"error"}`

### 症状

```
{"type":"error","message":"API Error: 400 [1211][模型不存在...]"}
agentproc:error:API Error: 400 ...
```

### 原因

被包装的 CLI 跑起来了，但自己报错。bridge 通过 `{"type":"error"}` 把错误转发给你（协议规定）。CLI 同时在 stderr 上以 `agentproc:error:<message>` 形式展示。

### 常见子情况

#### 模型不存在 / 无效模型

被包装的 CLI 调了一个你账号/端点上不存在的模型。

**修法：** 传正确的 model 环境变量。每个 profile 有自己的变量名（看对应 README）：
```bash
# claude-code:
agentproc hub run claude-code -p "hi" \
  --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  --env CLAUDE_MODEL="claude-sonnet-4-6"

# codex:
agentproc hub run codex -p "hi" \
  --env OPENAI_API_KEY=$OPENAI_API_KEY \
  --env CODEX_MODEL="gpt-5"
```

#### 缺 API key

**修法：** 通过 `--env` 传：
```bash
agentproc hub run claude-code -p "hi" --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

#### 鉴权过期 / 端点错

如果你在用代理或第三方端点（比如 Anthropic 的国内镜像），key 可能是有效的，但指向了错的 base URL。看被包装 CLI 自己的鉴权文档。

---

## agent 跑了但没返回内容

### 症状

`hub run` 退出码 0、没报错，但 stdout 是空的。

### 原因 & 修法

1. **流式模式下，agent 的回复全部通过 `{"type":"partial"}` 行输出。** 用了 `--quiet` 时 partial 被压制，你什么都看不到。重新跑时去掉 `--quiet`，或从 stderr 的 `agentproc:session:` 行确认 agent 确实回复了。
2. **被包装的 CLI 把所有内容写到了它自己的 stderr，没写到 stdout。** 某些 CLI 对警告这么做。用 `--verbose`（默认）跑，检查 stderr。如果你想把 stderr 也带进回复，在 profile 里设 `include_stderr_in_reply: true`。
3. **agent 退出码 0 但什么都没写。** 这是 agent 脚本本身的 bug，不是 AgentProc 的问题。直接把一个 turn 写进 stdin 看它实际行为：
   ```bash
   echo '{"type":"turn","message":"hi","session_id":"","from_user":"test","protocol_version":"0.3"}' | python3 ./bridge.py
   ```

---

## 超时（退出码 124）

### 症状

CLI 跑了一会儿，然后退出码 124，没有回复。

### 原因

agent 没在 `timeout_secs`（默认 1800 秒；某些 hub profile 设了 600 秒）内完成。

### 修法

- **agent 确实慢**（大代码库、大模型）：调大超时。
  ```bash
  agentproc hub run claude-code -p "..." --timeout 1800
  ```
- **agent 卡住了**（等交互输入、网络卡死）：被包装的 CLI 大概率想弹交互提示，而 AgentProc 无法响应。确认 profile 传了 `--dangerously-skip-permissions` 或等价的非交互 flag。hub profile 已经这么做了；如果你用自己的 profile，要自己确认。

---

## "Could not reach GitHub"

### 症状

```
error: could not reach GitHub while fetching hub profile

This is usually a transient network issue. Try: ...
```

### 原因

`fetch()` 自身抛了——DNS 失败、连接被拒、reset 等。**不是限流**。

### 修法

- 重试（瞬时故障通常几秒内恢复）。
- 如果在代理后面，设 `HTTPS_PROXY`：
  ```bash
  export HTTPS_PROXY=http://your-proxy:port
  ```
- 否则用本地仓库跑（看上面 "Hub 拉取失败 → 修法 3"）。

---

## 还是没解决

- [提 GitHub issue](https://github.com/jeffkit/agentproc/issues)——附上完整命令、完整 stderr 输出、`agentproc --version`。
- [`runner.js` 源码](https://github.com/jeffkit/agentproc/blob/main/sdk/node/src/runner.js)就是协议的代码化形式——读它就是读规范。
