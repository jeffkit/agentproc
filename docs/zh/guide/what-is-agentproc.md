# 什么是 AgentProc？

AgentProc 是一个通过进程接口将任意 Agent CLI 接入消息平台的极简协议。

## 它解决什么问题

你有一个 AI agent —— 也许是 Claude Code、Codex、自定义的 LLM 封装，或任何以 CLI 形式运行的工具。你想让用户通过消息应用（微信、Slack、Telegram 等）和它对话。中间的粘合层是最难的部分：

- 消息怎么传给 agent？
- 回复怎么传回来？
- 多轮对话的上下文怎么保持？
- 怎么流式推送响应让用户不用等？

AgentProc 用最简单的接口回答了这些问题：**环境变量输入，stdout 输出**。

## 工作方式

```
消息平台
    │
    ▼
  Bridge              ← 读取 profile YAML，管理进程生命周期
    │   环境变量
    ▼
你的脚本              ← 读取 AGENT_MESSAGE，处理，写入 stdout
    │   stdout
    ▼
  Bridge              ← 将回复转发给用户
```

bridge 在启动进程前注入上下文环境变量。你的脚本读取它们，调用任意 AI 系统，把响应写入 stdout。这就是整个协议。

## 它不是什么

- **不是 HTTP API。** 没有服务器需要运行，没有 endpoint 需要实现。
- **不是框架。** 你不需要继承任何类或实现任何接口。
- **不是平台特定的。** AgentProc 不感知微信、Slack 或任何消息平台。
- **不限定 AI。** 你可以调用 Claude、GPT、Gemini、本地模型，或简单的规则系统。

## 与 MCP 的对比

[MCP（Model Context Protocol）](https://modelcontextprotocol.io) 定义了 AI 模型如何调用外部工具。AgentProc 定义了消息平台如何调用 AI agent。它们解决不同的问题，可以互补 —— 你的 AgentProc 脚本内部完全可以使用 MCP 工具。
