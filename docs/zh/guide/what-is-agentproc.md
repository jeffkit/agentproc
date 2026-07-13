# 什么是 AgentProc？

AgentProc 是一个通过进程接口将任意 Agent CLI 接入消息平台的极简协议。

## 它解决什么问题

你有一个 AI agent —— 也许是 Claude Code、Codex、自定义的 LLM 封装，或任何以 CLI 形式运行的工具。你想让用户通过消息应用（微信、Slack、Telegram 等）和它对话。中间的粘合层是最难的部分：

- 消息怎么传给 agent？
- 回复怎么传回来？
- 多轮对话的上下文怎么保持？
- 怎么流式推送响应让用户不用等？

AgentProc 用最简单的接口回答了这些问题：**stdin 收一个 turn 对象，stdout 输出 NDJSON 事件**。

## 工作方式

```
消息平台
    │
    ▼
  Bridge              ← 读取 profile YAML，启动 agent 进程
    │   stdin 写一行 {"type":"turn",...}
    ▼
你的脚本              ← 读 turn，处理，向 stdout 写 NDJSON 事件
    │   stdout 每行一个 JSON 对象
    ▼
  Bridge              ← 将回复转发给用户
```

bridge 向 agent 的 stdin 写入一行 `{"type":"turn",...}`（消息、session id、附件……），然后关闭 stdin。你的脚本读取这行，调用任意 AI 系统，把响应作为 NDJSON 事件写回（`{"type":"partial"}` 流式、`{"type":"text"}` 最终正文、`{"type":"session"}` 声明 id）。这就是整个协议。

## 它不是什么

- **不是 HTTP API。** 没有服务器需要运行，没有 endpoint 需要实现。
- **不是框架。** 你不需要继承任何类或实现任何接口。
- **不是平台特定的。** AgentProc 不感知微信、Slack 或任何消息平台。
- **不限定 AI。** 你可以调用 Claude、GPT、Gemini、本地模型，或简单的规则系统。

## 与相关协议的对比

AgentProc 占据一个特定的生态位。相邻协议在*形态*上相似（子进程 + stdio），但在*目的*上不同。

### MCP — Model Context Protocol（Anthropic）

MCP 把一个 LLM 应用（客户端）连接到**工具和数据源**（服务器，一个子进程）。传输是 stdio 或 HTTP+SSE 上的 JSON-RPC 2.0。

**与 AgentProc 的关系：方向相反。** 在 MCP 中，AI 是客户端、工具提供者是子进程；在 AgentProc 中，bridge 是客户端、AI 包装器是子进程。它们自然组合——一个 AgentProc agent 内部完全可以使用 MCP 工具。

- 规范：https://modelcontextprotocol.io/

### ACP — Agent Client Protocol（Zed Industries）

ACP 把代码编辑器连接到 AI 编程 agent。传输是 stdio 上的 JSON-RPC 2.0，双向、长生命周期，假设一个交互式 IDE 会话，包含工具调用、文件 diff、模式切换。

**与 AgentProc 的关系：更丰富的表亲。** ACP 假设每次进程调用对应一个长期 IDE 会话；AgentProc 假设每次进程调用对应一个聊天回合。如果你在构建 IDE，用 ACP；如果你在把聊天机器人桥接到 CLI，用 AgentProc。

- 规范：https://agentclientprotocol.com/

### NDJSON / JSON Lines

NDJSON（每行一个 JSON 对象、换行分隔）是 Claude Code、Codex、Gemini CLI 流式模式内部使用的传输格式，也被 MCP 使用。

**与 AgentProc 的关系：AgentProc 0.3 本身就是 NDJSON 双向传输。** turn 作为一行 NDJSON 到达 stdin，agent 发出的每一行都是带类型的 JSON 事件（`partial`、`text`、`session`、`error`）。词汇表封闭且小，bridge 一行代码就能分类一行。代价是每行都必须是合法 JSON——裸 `echo "You said: hi"` 不再是合法 agent；SDK 会替你吸收这部分样板。

- 规范：https://jsonlines.org/

### AgentProc *不是* 什么

- **不是机器人框架。** Hubot、Errbot、BotKit 都活在 bridge 的*上游*（进程内适配器、HTTP 连接器）。AgentProc 定义的是 bridge 与 agent *之间* 的合约，与这些框架正交。
- **不是 agent 间协议。** A2A / AGNTCY 解决的是 agent 之间互相通信的问题。
- **不是 IDE 协议。** 那是 ACP 的领域。
- **不是工具协议。** 那是 MCP 的领域。
