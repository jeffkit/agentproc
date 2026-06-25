# 快速开始

5 分钟跑起一个 AgentProc 兼容的 agent。

## 第一步：写 agent 脚本

最简单的 agent 读取 `AGENT_MESSAGE`，然后把回复写到 stdout。

::: code-group

```bash [bash]
#!/usr/bin/env bash
# echo_agent.sh — 把用户发的消息原样回复
echo "你说：$AGENT_MESSAGE"
```

```python [python]
#!/usr/bin/env python3
# echo_agent.py
import os
print(f"你说：{os.environ['AGENT_MESSAGE']}")
```

```js [node]
#!/usr/bin/env node
// echo_agent.js
console.log(`你说：${process.env.AGENT_MESSAGE}`);
```

:::

## 第二步：创建 profile YAML

```yaml
# myagent.yaml
command: bash ./echo_agent.sh
timeout_secs: 10
```

## 第三步：本地测试

不需要启动 bridge，手动设置环境变量即可测试：

```bash
AGENT_MESSAGE="你好" \
AGENT_SESSION_ID="" \
AGENT_SESSION_NAME="default" \
AGENT_FROM_USER="test" \
AGENT_STREAMING="1" \
bash ./echo_agent.sh
```

预期输出：

```
你说：你好
```

## 第四步：接入 bridge

将 profile YAML 的路径告诉 bridge。具体步骤取决于你用的 bridge 实现，请参考对应 bridge 的文档。

---

## 下一步

- [阅读完整协议规范](/zh/spec/) 了解所有特性
- [使用 SDK](/zh/sdk/) 省去样板代码
- [查看示例](/zh/examples/claude) 了解如何接入 claude 等真实 AI agent
