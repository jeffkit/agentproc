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

## 错误处理

当 agent 遇到需要让用户看见的错误（上游 API 失效、限流、参数不合法等），输出一行 `AGENT_ERROR:` ——bridge 会把消息原样转发给用户，并把这次进程视为失败（即使后续退出码为 0）。比起单纯返回非零退出码，这能让用户看到一条有意义的说明，而不是 bridge 的通用模板。

### 裸脚本形式

任何能写 stdout 的脚本都可以直接输出协议行：

::: code-group

```bash [bash]
#!/usr/bin/env bash
# error_agent.sh
if [ -z "$AGENT_MESSAGE" ]; then
  printf 'AGENT_ERROR:"消息不能为空。"\n'
  exit 1
fi
echo "你说：$AGENT_MESSAGE"
```

```python [python]
#!/usr/bin/env python3
# error_agent.py
import os, json
msg = os.environ.get("AGENT_MESSAGE", "").strip()
if not msg:
    print(f'AGENT_ERROR:{json.dumps("消息不能为空。", ensure_ascii=False)}')
    raise SystemExit(1)
print(f"你说：{msg}")
```

```js [node]
#!/usr/bin/env node
// error_agent.js
const msg = (process.env.AGENT_MESSAGE || '').trim();
if (!msg) {
  process.stdout.write(`AGENT_ERROR:${JSON.stringify('消息不能为空。')}\n`);
  process.exit(1);
}
console.log(`你说：${msg}`);
```

:::

### SDK 形式

使用 SDK 时，调用错误透出 API 或抛出 `ProtocolError`，SDK 会替你格式化协议行。

::: code-group

```python [python]
from agentproc import create_profile, ProtocolError

async def handler(ctx):
    if not ctx.message.strip():
        raise ProtocolError("消息不能为空。")
    # 或者用：await ctx.send_error("消息不能为空。") 然后 return
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

```js [node]
const { createProfile, protocolError } = require('@agentproc/sdk');

createProfile(async (ctx) => {
  if (!ctx.message.trim()) {
    throw await protocolError('消息不能为空。');
    // 或者用：ctx.sendError('消息不能为空。'); return;
  }
  const reply = await myLLM(ctx.message);
  return { response: reply };
});
```

:::

详见 [协议规范](/zh/spec/#error-行) 和 [SDK 文档](/zh/sdk/python#错误透出)。

---

## 下一步

- [阅读完整协议规范](/zh/spec/) 了解所有特性
- [使用 SDK](/zh/sdk/) 省去样板代码
- [查看示例](/zh/examples/claude) 了解如何接入 claude 等真实 AI agent
