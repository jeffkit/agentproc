# AgentProc 文档站初学者体验评估

> 评估日期：2026-06-26
> 评估者视角：**无编程经验、但在指导下能用命令行**的初级 AI Agent 使用者
> 评估方法：只看 `https://agentproc.dev/` 的文档（不看源码），按文档指引在本地实跑
> 测试环境：macOS Darwin 25.5.0 · Node v20.19.3 · npm 10.8.2 · Python 3.14.5 · 已装 `claude` CLI · 已设 `ANTHROPIC_API_KEY`

---

## TL;DR

- **协议本身设计很好**：env-var 进、stdout 出、sentinel 行做流式/session/error，干净、可移植、易调试。我作为小白也能看懂原理。
- **核心功能完全可用**：在跳过若干坑之后，我用 `claude-code` profile 跑通了流式输出和多轮 session continuity，证明端到端链路是好的。
- **但首屏 5 分钟路径有几处会让小白直接劝退的卡点**，最严重的是「`-p` 短选项无效」「首次跑就被 GitHub 限流崩溃」「profile.yaml 里 `cwd: ~/your-project` 占位符没解释」。这些都是文档+CLI 一致性问题，**不需要改协议**就能修。
- **结论**：协议/SDK/bridge 的工程质量很高；**差的是文档的「初学者路径」打磨**和 **CLI 几个边界 bug**。下面给出具体清单和修复建议。

---

## 我实际跑通的路径（按时间线）

| # | 步骤 | 结果 |
|---|------|------|
| 1 | `npm install -g agentproc` | ✅ 724ms 装好，`--version` 报 `agentproc 0.3.0 (protocol 0.1)` |
| 2 | `agentproc hub list` | ✅ 列出 5 个 profile，和首页一致 |
| 3 | `agentproc hub run echo-agent -p "hello"` | ❌ 首次跑就被 GitHub API 403 限流，CLI 直接崩，吐 Node 堆栈 |
| 3' | `agentproc hub run echo-agent --prompt "hello"` | ✅ 改成长选项后跑通，输出 `You said: hello` |
| 4 | `agentproc --profile hub/echo-agent/profile.yaml --prompt hi` | ❌ 报 `python3 ENOENT`——因为 profile `cwd: .` 与执行目录不匹配 |
| 5 | 用 `claude-code` profile + 自定义 cwd + CLAUDE_MODEL | ✅ 流式输出、session ID、多轮续话全部正常 |

> 协议、bridge、SDK 这些底层都通过实测验证可用。问题集中在「初学者第一段路」。

---

## 卡点清单（按严重程度排序）

### 🔴 P0 — 阻断型（小白会直接放弃）

#### #1. `agentproc --help` 里完全没有 `hub` 子命令

- **现象**：首页第 ② ③ 步让我用 `agentproc hub run ...`，但 `agentproc --help` 输出的还是旧的 `--profile` 形式，没有任何 `hub` 字样。
- **小白视角**：我照首页输了 `hub run`，能跑。但我习惯性 `--help` 想看其他选项，看到的 help 完全对不上首页，会怀疑自己装错版本或者跑错命令。
- **建议**：
  - 顶级 `--help` 加上 `hub` 子命令章节（和 `agentproc hub --help` 现有的内容对齐）。
  - 或者把 `hub` 提到 help 头部，作为推荐入口；旧 `--profile` 路径放后面作为高级用法。

#### #2. 首次 `hub run` 在 GitHub 匿名限流时直接崩

- **现象**：第一次跑 `agentproc hub run echo-agent -p "hello"`，CLI 去拉 `jeffkit/agentproc:main`，被 GitHub API 403，整个命令直接抛 Node 堆栈退出：
  ```
  [agentproc] unhandled error: Error: GitHub API 403: {"message":"API rate limit exceeded..."}
      at httpGetJson (.../hub.js:86:11)
      ...
  ```
- **小白视角**：
  - 文档**从头到尾没提"hub profile 是从 GitHub 拉取的"**，所以小白根本不知道为什么会被 GitHub 限流。
  - 错误信息是英文 + Node 堆栈，对非程序员是天书。
  - 没有任何 fallback。其实本地仓库就有一份 `hub/echo-agent/`，CLI 完全可以本地读取，但它非要走网络。
- **建议**（按优先级）：
  1. **错误信息人性化**：把 `unhandled error: ...` 替换成「拉取 hub profile 失败：GitHub 限流。请稍后重试，或用 `agentproc hub install <name>` 离线安装，或直接用本地仓库 `--profile hub/<name>/profile.yaml`」。绝不输出 Node 堆栈给小白。
  2. **文档显眼处说明** profile 来源：首页 ② 节加一句"Profile 通过 GitHub 拉取，匿名有速率限制；企业/CI 环境请用 `hub install` 或本地仓库"。
  3. **支持 `GITHUB_TOKEN` 提限额**，并在文档里给出（一行 `--env GITHUB_TOKEN=...` 即可）。
  4. **（进阶）本地优先**：如果当前目录或上层存在 `hub/<name>/profile.yaml`，先用本地再 fallback 到远程。

#### #3. `hub run` 不接受 `-p` 短选项（但首页/help 全在用）

- **现象**：
  ```
  $ agentproc hub run echo-agent -p "hello"
  error: hub run requires --prompt <text> or --stdin
  ```
  换成 `--prompt` 立刻通过。
- **小白视角**：首页第 ③ 步原文就是 `-p "hello"`，原样复制粘贴不能跑，会以为产品坏了。
- **建议**：在 `hub run` 的参数解析里把 `-p` 别名加上（顶级 runner 已经支持 `-p`，这里只是漏了）。属于纯 bug fix。

#### #4. profile.yaml 模板的 `cwd: ~/your-project` 是占位符，但文档没说明

- **现象**：`hub/claude-code/profile.yaml` 写的是 `cwd: ~/your-project`，而 `~/your-project` 不存在。直接 `--profile` 跑会报 `spawn python3 ENOENT`（实际是 cwd 无效 + `./bridge.py` 相对路径找不到，Node spawn 把错都报到 argv[0] 上，超级误导）。
- **小白视角**：
  - README 只说「Edit `cwd:` in profile.yaml to point at the directory `claude` should work in」，没说**不改会怎样**、**占位符长什么样**、**应该改成什么**。
  - 我作为初学者把它当成"应该能跑的默认值"，结果崩在莫名其妙的地方。
- **建议**（择一或多做）：
  1. **hub 文档强调 `hub run` 默认 cwd = 当前目录**（hub/index.md 其实写了，但首页和各 profile README 都没说）。把这条放到首页第 ③ 节顶部：「`hub run` 会用你当前所在目录作为 agent 的工作目录，所以先 `cd` 到你的项目再跑」。
  2. **profile 模板留空 cwd**（`cwd:` 或 `cwd: ""`），让 runner 默认走当前目录。占位符 `~/your-project` 没有任何信息量，只是个坑。
  3. **README 顶部大字提示「两个改法」**：要么 `cd <你的项目> && agentproc hub run claude-code ...`，要么 `agentproc --profile ... --cwd <你的项目>`。两种都不需要改 YAML。

---

### 🟡 P1 — 体验型（不阻断但很烦）

#### #5. claude-code profile 的「模型字段名」三处不一致

- **现象**：
  - `hub/claude-code/README.md` 写的是 `CLAUDE_MODEL: "sonnet"`
  - `hub/claude-code/profile.yaml` 注释写的是 `ANTHROPIC_MODEL: "claude-sonnet-4-6"`
  - `hub/claude-code/bridge.py` 实际只识别 `CLAUDE_MODEL`
- **小白视角**：照 README 写 `CLAUDE_MODEL` 跑通了；但看到 profile 注释里又写 `ANTHROPIC_MODEL`，会怀疑哪个对、哪个被废弃了。
- **建议**：统一成 `CLAUDE_MODEL`（因为 bridge 实际用的就是它）。把 profile.yaml 的注释改成 `CLAUDE_MODEL`，并在 README 里明确「这个变量会被传给 bridge.py，bridge.py 再加 `--model` 传给 claude CLI」。

#### #6. 首页和 guide/getting-started 是**两条互不相关的快速上手路径**

- **首页**：`agentproc hub run echo-agent -p "hello"` —— 用 hub、零配置、强调 CLI。
- `guide/getting-started`：手写 `echo_agent.sh` + `myagent.yaml`，Step 3 还是直接 `AGENT_MESSAGE=... bash ./echo_agent.sh` 跑脚本，**完全绕开 agentproc CLI**。
- **小白视角**：从首页点「Get started」跳到 guide，会困惑「我到底该用哪条路径？为啥 guide 让我手写脚本，首页说 hub 一行通？」。两条路径在心智模型上**冲突**。
- **建议**：
  1. **统一主线**：guide 应该承接首页的 `hub run` 路径，先讲 hub，再讲"想自己写脚本？看这里"。
  2. **删除 guide Step 3 的"绕开 CLI 直接跑脚本"**，改成 `agentproc --profile ./myagent.yaml --prompt "hello"`，让 CLI 出现在每一步。
  3. 或者在 guide 顶部加一句分流：「只想用现成 agent？看首页 hub 路径。想自己写一个 agent 脚本？本页教你」。

#### #7. 「文档说会看到流式 partial」实际多数情况看不到

- **现象**：首页示例预期输出是：
  ```
  AGENT_PARTIAL:"This codebase is..."
  AGENT_SESSION:13c2f6ec-...
  ```
  但我跑短回复时只看到 `AGENT_SESSION:` + 最终回复，没有 `AGENT_PARTIAL:` 行。
- **小白视角**：会怀疑"流式是不是没工作？"。
- **建议**：在首页或 hub README 加一句说明「短回复可能一次吐完，看不到 partial 是正常的；长回复才会逐块 stream」。或者在示例上注明「示意，实际输出取决于回复长度」。

---

### 🟢 P2 — 打磨型（锦上添花）

#### #8. `agentproc hub run --help` 直接报"requires a profile name"

- 跟顶级 `--help` 行为不一致。小白习惯性敲 `hub run --help` 想看选项，结果被当成了缺 name 的运行调用。
- 建议：把 `--help/-h` 在子命令级别也识别。

#### #9. `agentproc hub install` 默认装到当前目录的 `./<name>/` 子目录

- 文档里这条命令的定位是"复制到当前目录便于编辑"，但默认行为是建子目录 `./echo-agent/`。小白预期可能是"装到当前目录"而不是"装到子目录"。
- 建议：文档里给个例子说清楚产物路径，或者加 `--flat` 选项。

#### #10. Python 用户在 macOS 上常遇到 `pip`/`pipx` 缺失

- 首页给了 npm/pipx/pip 三种安装方式。我的测试机只有 `python3`，没有 `pip` 也没有 `pipx`（macOS Homebrew Python 默认如此）。小白照 pipx 那条会卡在 `pipx: command not found`。
- 建议：在 pipx/pip 选项下面加一行「macOS 用户如果 `pip` 不可用，先 `python3 -m ensurepip` 或继续用 npm 路径」。或者直接把 npm 路径推成默认推荐（首页已经放第一位，挺好）。

#### #11. `spawn python3 ENOENT` 的错误归因很难

- 这个不是文档问题，是 runner 的错误处理问题：当 cwd 无效或脚本相对路径找不到时，Node spawn 把错误都报到 argv[0]（`python3`）上，让用户以为 python3 没装。
- 建议：runner 在 `spawn error: ENOENT` 时，先 stat 一下 `cwd` 是否存在、argv[0] 是否在 PATH 里，给出更准确的提示。

---

## 文档结构层面的建议

1. **首页的 5 分钟路径要"零失败"**。目前第 ③ 步的 `-p` 和 GitHub 限流会让 50% 的小白卡住。修好 P0 三条之后，首页路径就稳了。
2. **加一个「故障排除」页**（`/troubleshooting/`），集中收录：
   - GitHub 限流怎么办
   - `spawn ENOENT` 怎么办
   - 模型 404/400 怎么办
   - 怎么知道我的 profile 跑没跑通（`--verbose` / `--raw`）
3. **统一一条主线**：首页 → hub → CLI reference。guide/getting-started 重新定位为「想从零写一个 agent 脚本」的进阶页，不要和首页争"快速上手"的入口。
4. **每个 hub profile 的 README 顶部加一行 "Quick test"**：复制粘贴就能跑的命令（含 `--cwd` 用法），不要让小白读完一整页才知道怎么试。

---

## 我跑通 claude-code 的最终命令（供参考）

不需要改 profile.yaml，靠 `--cwd` 即可：

```bash
# 单轮
agentproc --profile hub/claude-code/profile.yaml \
  --prompt "what is this codebase? one sentence." \
  --cwd /path/to/your/project \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --env CLAUDE_MODEL="glm-4.6"

# 多轮：从 stderr 的 agentproc:session:<id> 抓 session id，下一轮 --session 复用
```

或用 hub 路径（更短，自动用当前目录作为 cwd）：

```bash
cd /path/to/your/project
agentproc hub run claude-code \
  --prompt "what is this codebase? one sentence." \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --env CLAUDE_MODEL="glm-4.6"
```

两种我都实测跑通，流式 + session 正常。

---

## 给优化工作的优先级建议

| 优先级 | 工作项 | 类型 |
|--------|--------|------|
| P0 | 修 `-p` 短选项 bug（#3） | 代码 1 行修复 |
| P0 | hub 拉取失败的人性化错误信息 + 文档说明 + GITHUB_TOKEN 支持（#2） | 代码 + 文档 |
| P0 | 顶级 `--help` 加 `hub` 子命令章节（#1） | 文档（CLI help 文案） |
| P0 | 把「`hub run` 默认 cwd = 当前目录」写到首页/profile README 显眼处；或干脆 profile.yaml 不写 `cwd:` 占位符（#4） | 文档 + 模板 |
| P1 | 统一 `CLAUDE_MODEL` 字段名（#5） | 文档/模板 |
| P1 | guide/getting-started 与首页主线对齐（#6） | 文档重写 |
| P1 | 加「故障排除」页 | 新增文档 |
| P2 | 子命令 `--help`、`hub install` 产物路径、ENOENT 归因、pipx 在 mac 上的提示 | 渐进打磨 |

P0 四条做完，小白首跑成功率应该能从当前的"看运气"提到 90%+。P1 是把"能跑"变成"用得舒服"。P2 是锦上添花。

---

# 二轮深度 Review（P0 修复完成后再走一遍）

> 评估日期：2026-06-26（同日）
> 评估者视角同上，但这次既包含"初级用户跑修复后产品"，也包含"维护者审代码"两个视角。
> 修复后的代码状态：所有 101 个 Node SDK 测试通过；echo-agent + claude-code 在本地端到端实测跑通。

## 已修复的部分（P0 + 顺手修了的 P1/P2）

### P0 — 4 条全部修复并复测通过

| # | 问题 | 修法 | 验证 |
|---|------|------|------|
| #1 | `hub run -p` 被当 positional | `cli.js` 在 hub 解析层把 `-p` 标准化为 `--prompt` 再传给主 parser | `agentproc hub run echo-agent -p "hi"` 一行直出 |
| #2 | GitHub 限流崩溃 + Node 堆栈 | 引入 `HubError`（hub.js），CLI main catch 友好打印 + 给出 3 条解决路径 | mock 403 验证：可读 hint，无堆栈 |
| #3 | 顶级 `--help` 没 hub 子命令 | 重写 `showHelp`：开头就是 hub 三行命令，profile 模式放后面作"Advanced" | 实测 help 输出 hub 章节在最上面 |
| #4 | `cwd: ~/your-project` 占位符 | 引入 `{{PROFILE_DIR}}` 占位符（runner substitute），所有 5 个 profile 改用 `python3 {{PROFILE_DIR}}/bridge.py`，删除 cwd 字段；hub run 默认 cwd = `process.cwd()` | echo-agent 从 `/tmp` 直接跑通 |

### P1/P2 顺手修掉的

- **🟡 exit code 静默归零 bug**（深度 review 才发现）：`main()` 是 async function 返回 exit code，但 `.then` 没接 resolve 值。导致 `hub runn`（拼错子命令）、`hub run` 缺 prompt 等错误路径 exit code 都是 0，shell 无法判断成败。修法：`.then(code => process.exit(code))`。
- **🟡 `--refresh` 跟 hub run 一起用就抛 unknown option**（评估时发现）：parseArgs 不认识 `--refresh`，hub run 走 parseArgs 就崩。修法：hub 分发层自己解析 flag，不调 parseArgs，把 `--refresh` 单独识别。
- **🟡 `hub run --help` 报"requires a profile name"**（评估 P2 卡点 #8）：在 hub dispatcher 加 `rest.includes('--help')` 分支，所有 hub 子命令都识别 `--help/-h`。
- **🟢 CLAUDE_MODEL 字段名不一致**（评估卡点 #5）：claude-code/profile.yaml 注释里的 `ANTHROPIC_MODEL` 改成 `CLAUDE_MODEL`，跟 bridge.py 和 README 一致。
- **🟢 `spawn ENOENT` 错误归因**（评估卡点 #11）：runner 新增 `diagnoseSpawnError()`，区分 cwd 不存在/不是目录/权限不足、命令不在 PATH、命令路径无效、argv 文件参数找不到 4 种情况，给出针对性 hint。实测 bad cwd 和 bad cmd 都给出清晰提示。
- **🟢 `npm test` 漏跑 hub.test.js**：package.json test script 加上。
- **🟢 Python/Node SDK 文档对齐**：guide/getting-started Step 3 改用 `agentproc --profile` 测试，不再绕开 CLI。
- **🟢 中文站同步**：所有 zh 文档对应英文版的修改都做了同步。

### 总改动量

```
23 files changed, 701 insertions(+), 132 deletions(-)
```

代码改动主要集中在：
- `sdk/node/src/cli.js` (+142) — hub 解析重写、help 重构、exit code 修复
- `sdk/node/src/hub.js` (+100) — HubError + authHeaders + 4 种错误归因
- `sdk/node/src/runner.js` (+109) — `{{PROFILE_DIR}}` substitute + 相对 cwd 解析 + diagnoseSpawnError

文档改动覆盖：英文/中文首页、guide、cli、hub、spec、5 个 hub profile README。

## 仍未修复的部分（按优先级）

### 🟡 P1 — 建议尽快跟进

#### R1. 拼错 profile 名时，错误地跑去 GitHub 拉取

- **现象**：`agentproc hub run nonexistent-name` 不会先校验名字，直接 fetch → 限流时返回 403，用户以为是网络问题，其实是名字错了。
- **当前缓解**：错误 hint 里加了一行 "Not sure the profile name is right? Check with: `agentproc hub list`"，但用户体验仍然不直接。
- **建议修法**：fetchProfile 在拉之前先调一次 `listProfiles`（有 1h 内的 cache，几乎不耗 API），如果 name 不在列表，直接返回 "unknown profile"。
- **代价**：每次 `hub run` 多一次（缓存命中的话不耗 API）调用。

#### R2. Python/Node SDK 文档的 "Local testing" 段还用绕开 CLI 的方式

- **现象**：sdk/python.md 第 138 行、sdk/node.md 类似位置仍写 `AGENT_MESSAGE=... python3 ./agent.py`，跟 guide/getting-started 已经统一的"用 agentproc CLI 测试"路线不一致。
- **建议**：补一段 "Or test through the agentproc CLI: `agentproc --profile ./myagent.yaml --prompt hi`"。

#### R3. 缺一个 "故障排除" 页

- 评估里就提过。现在错误信息已经友好很多（HubError、spawn 归因），但用户可能想集中查阅"遇到 X 怎么办"。建议加 `/troubleshooting/`，把 GitHub 限流、ENOENT、agent 自身报错（如 model not found）、cwd 问题、token 配置这几类集中讲清。

### 🟢 P2 — 渐进打磨

#### R4. Python 用户在 macOS 上 `pip`/`pipx` 常缺失

- 首页 npm/pipx/pip 三选项里，pipx/pip 在 mac 上常常装不上（Homebrew Python 默认无 pip）。
- 建议：在首页 pipx/pip 选项下加一行注释「macOS 用户如果 `pip` 不可用，运行 `python3 -m ensurepip`，或继续用 npm 路径」。

#### R5. cli.js 的 `verbose` 表达式冗余

- cli.js:331 `opts.verbose || !opts.quiet || (opts.verbose === undefined && opts.quiet === undefined) || opts.verbose`——同一变量出现两次，逻辑可简化为 `opts.verbose !== false`。
- 不影响功能，纯粹 cleanup。

#### R6. `agentproc hub install` 默认装到 `./<name>/` 子目录

- 评估卡点 #9。文档已说明，但用户预期可能是装到当前目录文件而不是子目录。建议在 hub install 的输出里多一句"installed 4 files to: ./echo-agent/"，让用户清楚产物结构。

#### R7. hub profile README "Local test" 段还展示老的 cwd 占位符场景

- `hub/claude-code/README.md` 的 Local test 段写 `cd hub/claude-code && AGENT_MESSAGE=... python3 bridge.py`，绕开 agentproc CLI。可以补充一行"或通过 CLI 测试：`agentproc --profile ./hub/claude-code/profile.yaml --prompt hi --cwd <your-project>`"。

#### R8. CLAUDE_DISALLOW_TOOLS 的默认值在 README 和 profile.yaml 注释里没对齐

- README 说默认 `AskUserQuestion`，profile.yaml 没注释这个 env。建议在 profile.yaml 的 env 段加注释。

## 复测路径（给后续 PR review 用）

```bash
# 1. 装好仓库依赖（已装 npm 全局包的用户需要重新装一次拿新版）：
npm install -g agentproc  # 或者直接用仓库源码: node sdk/node/src/cli.js ...

# 2. 冒烟测试（如果 GitHub 没限流）：
agentproc hub run echo-agent -p "hello"
# 期望: You said: hello

# 3. 真实 agent（需要 ANTHROPIC_API_KEY + claude CLI）：
cd ~/projects/your-app
agentproc hub run claude-code -p "what is this codebase?" --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
# 期望: 看到 AGENT_SESSION: + 回复正文

# 4. 多轮 session：
SESSION=<上面 stderr 里 agentproc:session: 后那串>
agentproc hub run claude-code -p "tell me more" --session $SESSION --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY

# 5. 故障路径复测：
agentproc hub run echo-agent --prompt hi --cwd /nonexistent
# 期望: "profile.cwd does not exist: /nonexistent. Pass --cwd ..."

agentproc hub runn echo-agent -p hi
# 期望: "unknown hub subcommand: runn", exit code = 2

agentproc --help | head -5
# 期望: 头部看到 hub 三行命令
```

## 结论

修复后**首跑成功率从"看运气"应该能到 95%+**。最关键的 4 个 P0 全部解决，额外修了 3 个 P1（exit code、--refresh、子命令 help）和 4 个 P2（CLAUDE_MODEL 一致性、ENOENT 归因、SDK 文档对齐、npm test 漏跑）。

剩余 8 条优化点都是 P1/P2，不影响小白跑通主路径，但能进一步提升体验。建议下一轮迭代优先做 R1（拼错名字的错误归因）和 R3（故障排除页）——这两个对"卡住后能否自救"影响最大。

---

# 二轮优化完成（R1-R8 + 三轮 review 又修了 3 个）

> 跟进日期：2026-06-26（同日）
> 状态：上面列出的 8 条 P1/P2 全部修复并复测通过；三轮 review 又发现并修了 3 个新问题。

## 全部完成项

| # | 标题 | 类型 | 修法 |
|---|------|------|------|
| R1 | 拼错 profile 名的友好归因 | P1 代码 | `fetchProfile` 在 getTree 成功但目录不存在时抛 `HubError`，附可用名字列表 + "Did you mean" 建议（prefix match + edit distance 双路径） |
| R2 | SDK 文档测试段对齐 | P2 文档 | Python/Node SDK 的 "Local testing" 段改成"用 agentproc CLI 测试"，绕开 CLI 的方式放进折叠区 |
| R3 | 新增故障排除页 | P1 文档 | 新增 `docs/guide/troubleshooting.md` + 中文版，sidebar 收录，首页和 guide 末尾加链接。覆盖限流/名字错/spawn ENOENT/agent 报错/超时/网络断 6 大场景 |
| R4 | mac pipx/pip 缺失提示 | P2 文档 | 首页 EN/ZH 安装段加 tip：「macOS 用户 pip 不可用时用 ensurepip 或直接走 npm」 |
| R5 | verbose 表达式 cleanup | P2 代码 | `opts.verbose !== false` 替换原来的四段冗余表达式 |
| R6 | hub install 输出说明 | P2 代码 | install 后多打印一行 `Next: edit <path> if you want, then run: agentproc --profile <path> --prompt "hi" --cwd <your-project>` |
| R7 | hub README 测试段对齐 | P2 文档 | 5 个 hub profile README 的 Local test 段全部改成"用 hub run 测试 + 折叠区放裸脚本"，顺手修了 claude-code README 里 `AGENT_PARTIAL,",..."` 缺引号的笔误 |
| R8 | claude-code/codex env 注释补全 | P2 文档 | profile.yaml env 段补 `CLAUDE_DISALLOW_TOOLS` / `CODEX_MODEL` 注释 |

## 三轮 review 又修的 3 个

- **R9：typo 建议阈值对短输入太严**——`claude`/`echo` 这种常见漏后缀的输入得不到建议。修：suggestCloseName 增加 prefix match 路径（候选唯一以 input 为前缀时直接返回），edit distance 阈值按长度分级（≤6→1, 7-12→2, >12→3）。复测：`claude`→`claude-code`, `echo`→`echo-agent`, `calude-code`→`claude-code` 全部命中。
- **R10：agent 退出非零但 stderr 含"file not found"时无归因**——这是被包装 CLI 启动后才发现的脚本缺失（spawn 自己成功），原来的 diagnoseSpawnError 不会触发。修：runner 新增 `diagnoseStderrFailure()`，在 child 退出码非 0、AGENT_ERROR 未发时扫累积 stderr，识别 python 的 "can't open file"、node 的 "Cannot find module"、bash 的 "No such file or directory"、通用 errno 2 共 4 种模式，匹配则升级为 AGENT_ERROR。实测 `python3 {{PROFILE_DIR}}/nope.py` 触发 `agent script not found: /tmp/nope.py. Check the profile's command path (likely a {{PROFILE_DIR}} issue or a typo).`
- **R11：stderr 累积无上限 + slice 头部丢尾部**——为支持 R10 引入 `stderrAll` 累积变量，code reviewer 指出：（a）无 cap 会内存膨胀；（b）若 cap 截头部，长输出 noisy agent 的真正错误出现在尾部时会被丢。修：8KB 滑动窗口（保留最近 8KB 而非最早），同时把 python `can't` regex 扩展为 `(?:can'?t|cannot)` 兼容非英文 locale。

## 最终改动量

```
30 files changed, ~1100 insertions(+), ~180 deletions(-)
```

代码：cli.js / hub.js / runner.js 累计新增约 380 行。文档：新增 troubleshooting.md（EN+ZH），更新首页/guide/cli/hub/spec/sdk 共 12 个文件 + 5 个 hub profile README。

## 最终测试状态

- **101/101** Node SDK 测试通过（runner/hub/index 三套）
- **echo-agent** 从任意目录用 `-p` 跑通
- **claude-code** 跑通流式 + 多轮 session continuity
- **故障路径**：限流 / 拼错名字（带建议）/ 拼错子命令 / bad cwd / bad command / agent 脚本不存在 / agent 模型错 全部给出可读 hint，exit code 正确（0/1/2/124）

## 还能进一步打磨的（P3，不影响发布）

- `stdoutBuf` 仍然无上限（与 `stderrBuf` 同样属于历史设计）；大输出 agent 已有 `max_reply_chars` 截断兜底，所以不会真崩，但不优雅。
- `agentproc hub install` 不支持 `--flat` 选项（评估卡点 #9）；产物路径文档已说清，可接受。
- `R10` 的 `diagnoseStderrFailure` 只覆盖 4 种模式；如果将来 bridge 用其他语言写（perl/ruby），可能要补对应模式。

## 结论

修复完成。**首跑成功率应该接近 100%**——所有可预见的"卡住"场景都有针对性提示和自救路径。可以提交。

