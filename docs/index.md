---
layout: home

hero:
  name: AgentProc
  text: Connect any Agent CLI to any messaging platform
  tagline: A minimal process-based protocol. No HTTP, no sockets — just stdin, stdout, and env vars.
  actions:
    - theme: brand
      text: Quick Start
      link: /guide/getting-started
    - theme: alt
      text: Read the Spec
      link: /spec/

features:
  - icon: ⚡
    title: Zero dependencies
    description: Any script in any language that reads env vars and writes to stdout is a valid AgentProc agent.
  - icon: 🔄
    title: Session continuity
    description: Built-in session handoff via AGENT_SESSION lines. Your agent owns the session logic; the bridge just stores and forwards.
  - icon: 📡
    title: Streaming out of the box
    description: Emit AGENT_PARTIAL lines to stream responses to users in real time, without any HTTP or WebSocket setup.
  - icon: 🧩
    title: Platform agnostic
    description: AgentProc doesn't know about WeChat, Slack, or any specific platform. The bridge adapts the protocol to wherever your users are.
---
