---
layout: home

hero:
  name: AgentProc
  text: 将任意 Agent CLI 接入任意消息平台
  tagline: 极简的基于进程的协议。没有 HTTP，没有 socket —— 只有 stdin、stdout 和环境变量。
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/guide/getting-started
    - theme: alt
      text: 阅读规范
      link: /zh/spec/

features:
  - icon: ⚡
    title: 零依赖
    description: 任何能读取环境变量并写入 stdout 的脚本，无论用什么语言，都是合法的 AgentProc agent。
  - icon: 🔄
    title: 会话续接
    description: 通过 AGENT_SESSION 行内置会话传递。你的 agent 掌管 session 逻辑，bridge 只负责存储和转发。
  - icon: 📡
    title: 开箱即用的流式输出
    description: 输出 AGENT_PARTIAL 行即可实时向用户推送响应，无需任何 HTTP 或 WebSocket 配置。
  - icon: 🧩
    title: 平台无关
    description: AgentProc 不感知微信、Slack 或任何具体平台。bridge 负责将协议适配到用户所在的地方。
---
