# Handoff — AgentProc 架构 review 后续

这份文档给下一个会话接手用。上一个会话对 AgentProc 做了一次毒舌架构 review，并落地了「最该先动的三件事 + spec 同步」。本文记录：上下文、已完成的改动、当前状态、**还没动的 review 项**（按优先级排）、以及接手时必须知道的坑。

> 写于 2026-07-02。对应 SDK 版本 `0.5.2`、spec doc `0.7`、wire protocol `0.1`。本会话**未提交 git**——改动全在工作区，下一个会话自行决定何时 commit。

---

## 0. 先读这些才有上下文

- `AGENTS.md` —— 仓库规矩（两 SDK「mirror not lockstep」、版本 bump 联动、不要手写 YAML parser、Conventional Commits、提交不带 Co-authored-by）。
- `spec/protocol.md` + `spec/protocol.zh.md` —— 协议是 source of truth，中英必须镜像。
- `CHANGELOG.md` —— 三条版本轨（wire / doc revision / SDK pkg）。本会话在 Unreleased 下加了三个 `0.5.2` section，已写清 why/what/impact。
- 原始 review 的 16 条结论没有单独存档，但每条都映射到下面的「未完成项」，并指了代码行。

---

## 1. 本会话已完成（三件大事 + spec/CI 同步）

### A. Python 手写 YAML parser → PyYAML
- 新增 `sdk/python/src/agentproc/yaml.py`（`parse_yaml` 走 `yaml.safe_load`，`parse_yaml_simple` 别名）。
- `sdk/python/src/agentproc/cli.py` 删掉 138 行手写 parser（`_leading_spaces`/`_strip_scalar`），改为 `from .yaml import parse_yaml`，清掉未用的 `Tuple`/`Any` import。
- `sdk/python/src/agentproc/hub.py` 把函数内 `from .cli import parse_yaml  # avoid cycle` 改成顶层 `from .yaml import parse_yaml`，顺手修正「Zero dependencies」过时文档。
- `sdk/python/pyproject.toml`：`dependencies = ["PyYAML>=5.1"]`，`version 0.5.1 → 0.5.2`。
- 新增 `sdk/python/tests/test_yaml.py`，钉死被修掉的 bug（inline `#` comment、空 env 值为 `None`、block scalar、flow sequence、嵌套 map、JSON 输入）。
- `AGENTS.md` 的「Don't hand-roll a YAML parser」改写，记上 retired 两次的黑历史。

### B. `env_allowlist` 从化妆品变成真信任边界
- `sdk/node/src/runner.js` + `sdk/python/src/agentproc/runner.py` 各加 `ENV_INFRA_VARS` + `buildBaseEnv`/`build_base_env`。
  - allowlist **缺省** → 子进程整包继承 `process.env`/`os.environ`（向后兼容，现有 profile 零改动）。
  - allowlist **存在** → 子进程 env = infra 集（`PATH`/`HOME`/`TERM`/… + Windows `SystemRoot`/`TEMP`/…）+ profile.env（allowlist 过滤）+ `AGENT_*` + CLI `--env`。bridge 手上未声明的密钥不再泄漏。
  - `${VAR}` 拦截 + stderr 警告行为不变。
- 两个 runner 各加两条测试：未声明密钥不泄漏（`sdk/node/src/runner.test.js`、`sdk/python/tests/test_runner.py`）；缺省时仍整包继承。
- spec `env_allowlist` section 重写（EN+ZH），枚举 infra 集，明确「两件事改变」。

### C. SDK 入口的跨语言一致性测试
- 新增 `spec/conformance/sdk.json`（5 场景：返回 string / 返回 `AgentResult` / 返回 `None`+`send_partial` / 抛 `ProtocolError` / `send_error` 后返回 body）。
- Node：`sdk/node/src/sdk_harness.js` + `sdk/node/src/sdk.test.js`（加进 `package.json` 的 `test`）。
- Python：`sdk/python/tests/_sdk_harness.py` + `sdk/python/tests/test_sdk.py`。
- `spec/conformance/README.md` 登记 `sdk.json` 及映射。

### D. 版本 / spec / CI 同步
- `sdk/node/package.json` `0.5.1 → 0.5.2`。
- `spec/protocol.md` / `spec/protocol.zh.md` doc revision `0.6 → 0.7`，各加 Changelog 条目。
- `CHANGELOG.md` 版本轨头更新 + 三个 `0.5.2` section（含 hub profile 影响说明）。
- `.github/workflows/test.yml` + `publish.yml`：Node 测试列表加 `src/sdk.test.js`，conformance job 加 `src/sdk.test.js` + `tests/test_sdk.py`，所有跑 Python 测试的 step `pip install pytest` → `pip install pytest pyyaml`。

### 当前状态
- Node 全套 **184 passed**；Python 全套 **223 passed**。
- 版本一致性脚本通过：pkg 两边 0.5.2、protocol 两边 0.1、入口点未重声明 literal。
- ruff：新文件全 clean。`runner.py`/`hub.py`/`cli.py` 里有 **7 个 F401 是既有未用 import**（`runner.py` 的 `sys`/`Tuple`/`Union`、`hub.py` 的 `os`、`cli.py` 的 `EXIT_ERROR`/`EXIT_SUCCESS`/`EXIT_TIMEOUT`），**非本会话引入，按规矩没动**——下一个会话想清就清，但别混进这次 commit。
- 改动未提交。`git status --short` 见本会话最后输出。

---

## 2. 还没动的 review 项（按优先级排）

### 优先级高 —— 会碰线协议，必须先评估兼容性

**R1. `command`/`args` 的「字段存在性」语义过度设计**
- 现状：`args` 缺失 → split `command`；`args: []`（显式空）→ 不 split；`args:` (null) → 当缺失。三种状态两种含义，靠 `hasOwnProperty` + `!= null` 双重判定。
- 代码：`spec/protocol.md:134-157`、`sdk/node/src/runner.js:89-92`、`sdk/python/src/agentproc/runner.py:123-142`。
- 建议方向：要么强制 `command` 永远单 token、多 token 一律走 `args`；要么加显式 `split: false`。**这是 breaking change**，需要 wire protocol 或至少 profile schema 版本协调，所有 hub profile 的 `command: python3 {{PROFILE_DIR}}/bridge.py` 都得改。先开 issue 讨论再动。

**R2. `AGENT_*:` 前缀冲突 + 「加空格前缀」甩锅给 agent**
- 现状：reply body 不得以 `AGENT_SESSION:`/`AGENT_PARTIAL:`/`AGENT_ERROR:` 开头；agent 想原样输出这类文本得手动加空格前缀。且 spec 允许 bridge「match stripped 或 match raw」两种实现，跨 bridge 行为不一致。
- 代码：`spec/protocol.md:216-228`、`sdk/node/src/runner.js:318-332`、`sdk/python/src/runner.py:352-359`。
- 建议方向：统一所有 bridge 的判定策略（推荐 raw match，禁止 stripped match），spec 把「SHOULD be consistent」升级成 MUST。会打破任何依赖 stripped match 的第三方 bridge——先调研再动。

**R3. `AGENT_PARTIAL:`/`AGENT_ERROR:` 的 lenient/strict JSON 双轨**
- 现状：解析失败时 lenient bridge 回退 raw、strict bridge 丢弃，spec 允许两种，default SHOULD lenient。同一 agent 输出在两种 bridge 下行为不同。
- 代码：`spec/protocol.md:259-265`、`sdk/node/src/runner.js:284-299`、`sdk/python/src/agentproc/runner.py:337-349`。
- 建议方向：强制 lenient（删 strict option）或强制 strict。删 strict 是收紧但破坏性小（没人真用 strict）。同步两 runner + conformance `cases.json`。

### 优先级中 —— 不碰线协议，纯实现/测试补强

**R4. hub bridge 的「observable parity」无自动化兜底**
- 现状：`recursive/bridge.py` 354 行 vs `bridge.js` 328 行，各一套 session-dir 持久化；conformance suite 不测 hub bridge。`recursive/bridge.js` 甚至不读任何 `RECURSIVE_*` env（与 .py 不一致，疑似既有 parity bug）。
- 建议方向：给 hub bridge 写跨语言 e2e fixture（mock CLI 的 NDJSON 输出 → 断言一致的 `AGENT_*` 输出 + 退出码）。先从 `recursive` 的 `.py`/`.js` 分歧查起。

**R5. runner 的 stderr 无界缓冲 + 正则诊断**
- 现状：`stderrFull`/`stderr_full` 无界增长，靠 4 个手写正则诊断失败；Node/Python 各抄一份正则且要求措辞 parity。
- 代码：`sdk/node/src/runner.js:138-170, 504-521`、`sdk/python/src/agentproc/runner.py:294-330, 450-465`。
- 建议方向：给 stderrFull 加上限（如 1MB 滚动窗口）；把正则诊断抽成共享数据（`spec/conformance/` 里放 (pattern, hint) 表，两 runner 各自消费）。

**R6. `session_file_path` 三重冗余校验且互相不一致**
- 现状：runner 侧 `SESSION_ID_RE` 已卡死字符集，不可能含 `/`/`..`；Node `index.js:59` 用 `includes('..')` 误伤合法 id `a..b`；Python `__init__.py:184` 用子串 `in` 同样误伤。
- 建议方向：入口校验统一改成 `if !SESSION_ID_RE.test(id) throw`，删掉手写的 `/`/`\`/`..` 检查。两边一致且不误伤。

**R7. `create_profile`/`createProfile` 的 sync/async 分歧**
- 现状：Python 强制 async（`asyncio.run(handler(ctx))`，sync handler 直接炸）；Node 兼容 sync/async。本会话已在 `sdk.json` fixture 描述里**钉住**这个分歧（全用 async handler 让两边过），但**没修**。
- 建议方向：要么 Node 也强制 async（breaking），要么 Python 也兼容 sync（用 `asyncio.run` 包裹时检测 `inspect.iscoroutine`，非协程直接同步调用）。后者更友好。

**R8. CLI arg 解析手卷两遍 + flag 列表硬编码复制**
- 现状：`sdk/node/src/cli.js` 的 `parseArgs` 是 switch-case；hub 子命令又手卷一遍并把 runner flag 列表硬抄一份（`takesValue = a === '--prompt' || ...`，`cli.js:152-156`）。加 flag 要改三处，漏一处静默吞掉。Python 侧用 argparse 反而干净。
- 建议方向：Node 引入 `commander`/`yargs`，或把 flag 元数据抽成单表两边消费。权衡「zero-dep」执念 vs 维护成本。

### 优先级低 —— 清理 / 文档瘦身

**R9. 版本号读取重复**：`__init__.py:_read_version` 与 `cli.py:_read_pkg_version` 几乎一字不差。抽成 `_version.py` 单点导出。

**R10. timeout 嵌套 setTimeout 的 race**：`runner.js:543-556` SIGKILL timer 不取消；进程在 grace 内自退时 `timedOut` 仍 true → exitCode 强制 124。spec 对「kill 已发但进程自退」算不算 timeout 含糊。两 SDK 行为需对齐 + spec 澄清。

**R11. Python runner 用 thread+轮询 drain stdio**：`runner.py:507-613`，2026 年还在 thread 模拟非阻塞。Node 天然事件循环。parity 只在 happy path 成立。可考虑切 `asyncio.subprocess`（但要兼顾 `requires-python>=3.9`）。

**R12. spec 文档冗长**：`AGENT_PROTOCOL_VERSION` 一个不携带信息的字段配了 7 行「别用它」规范；`Comparison with Related Protocols` 60 行「我不是 MCP/ACP/LSP」辩护词。可瘦身一半。AGENTS.md 嘱咐「Don't rewrite casually」——这条要的是克制，不是不能动。

---

## 3. 接手必读的坑

1. **未提交**。本会话所有改动在工作区。`git status` 有 15 个 M + 7 个 ??。决定 commit 时按 Conventional Commits，**不要带 Co-authored-by**（用户规矩）。建议拆成 3 个 commit：`fix(sdk): env_allowlist real trust boundary`、`refactor(sdk/python): replace hand-rolled YAML parser with PyYAML`、`test: add SDK entry cross-implementation conformance`，spec/CI/CHANGELOG/AGENTS 跟着各自归属。

2. **既有 7 个 ruff F401 不是本会话引入**。别混进本次 commit；要清单独一个 `chore: remove unused imports`。

3. **hub profile 的 env 旋钮 nuance**（B 项的连带影响）：所有 hub bridge 读 config 旋钮都有安全默认值，**没有硬故障**。但 `AGY_TIMEOUT`/`AGY_DANGEROUSLY_SKIP_PERMISSIONS`/`CODEBUDDY_DISALLOW_TOOLS`/`QWEN_SANDBOX` 没在各自 profile 的 `env_allowlist` 里——以前 shell 设了会偷偷漏进去，现在要走 profile 的「uncomment to use」。**不要给 `AGY_DANGEROUSLY_SKIP_PERMISSIONS` 加 `${VAR}` 声明**：uncomment 但未设值会注入空字符串，bridge 的 `env.get(..., "1")` 拿到空 → 关掉默认的 `--dangerously-skip-permissions`，反而更危险。详见 `CHANGELOG.md` 的 env_allowlist section。

4. **版本 bump 联动**（AGENTS.md 规矩）：动 SDK 包版本要同步 `pyproject.toml` + `package.json` + `CHANGELOG`。动 wire 字节才动 `spec/protocol.md` 的 `**Version:**` 和 `PROTOCOL_VERSION` 常量。`PROTOCOL_VERSION` 单一来源是 `runner.*`，入口点只 re-export，**不要 copy literal**——CI 有 `version-consistency` job 验证。

5. **两 SDK 是 mirror not lockstep**：observable 行为要对齐（conformance suite 兜底），实现细节允许分歧。改 spec-relevant 行为要双边改 + 扩 conformance。改友好提示字符串不必双边同步。

6. **中英 spec 必须镜像**：`spec/protocol.md` 改了，`spec/protocol.zh.md` 要跟。docs/ 下每页也有 `docs/zh/` 镜像（本会话没动 docs，因为 review 项没涉及）。

7. **conformance 套件三层**：`cases.json`→`classify_line`/`classifyLine`；`scenarios.json`→`run()`；`sdk.json`→SDK 入口（子进程）。改规则前先加 case 让测试红，再改实现。`spec/conformance/README.md` 有完整说明。

8. **CI 跑 Python 测试现在要 `pip install pytest pyyaml`**（本会话已改 test.yml + publish.yml）。漏装会导致 `agentproc.yaml` import 失败。

9. **`recursive/bridge.js` 不读 `RECURSIVE_*` env**——疑似既有 hub parity bug（.py 读、.js 不读）。接 R4 时先查这个。

---

## 4. 建议的下一个会话起手式

1. `cd sdk/node && npm test` + `cd sdk/python && PYTHONPATH=src pytest -q tests/` 确认起点绿。
2. 挑 R1～R3 中的一个，先在 spec 开 issue/草案讨论兼容性（都碰线协议）。
3. 或挑 R4/R6/R7 中一个，纯实现层、不碰线协议，可直接开工 + 扩 conformance。
4. 若要先提交本会话成果，按第 3 节第 1 条的 commit 拆分来。
