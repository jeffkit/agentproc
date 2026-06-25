# SDK 概览

AgentProc SDK 封装了读取环境变量和写入 stdout 协议的样板代码，让你专注于 agent 逻辑。

## 可用 SDK

| 语言 | 包名 | 安装 |
|------|------|------|
| Python | `agentproc` | `pip install agentproc` |
| Node.js | `@agentproc/sdk` | `npm install @agentproc/sdk` |

## 不用 SDK

你不需要 SDK。任何读取 `AGENT_*` 环境变量并写入 stdout 的脚本都可以工作。参见[裸脚本示例](/zh/examples/bare)。

## 用 SDK

写一个异步函数，SDK 处理其余的一切。

::: code-group

```python [Python]
from agentproc import create_profile

async def handler(ctx):
    reply = await my_llm(ctx.message)
    return reply

create_profile(handler)
```

```js [Node.js]
const { createProfile } = require('@agentproc/sdk');

createProfile(async ({ message }) => {
  const reply = await myLLM(message);
  return { response: reply };
});
```

:::

- [Python SDK 文档](/zh/sdk/python)
- [Node.js SDK 文档](/zh/sdk/node)
