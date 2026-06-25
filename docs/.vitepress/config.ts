import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'AgentProc',
  description: 'A minimal protocol for connecting any Agent CLI to a messaging platform',
  lang: 'en-US',

  base: '/agentproc/',

  locales: {
    root: {
      label: 'English',
      lang: 'en-US',
    },
    zh: {
      label: '中文',
      lang: 'zh-CN',
      themeConfig: {
        nav: [
          { text: '快速开始', link: '/zh/guide/getting-started' },
          { text: '协议规范', link: '/zh/spec/' },
          { text: 'SDK', link: '/zh/sdk/' },
          { text: '示例', link: '/zh/examples/' },
        ],
        sidebar: {
          '/zh/': [
            {
              text: '简介',
              items: [
                { text: '什么是 AgentProc？', link: '/zh/guide/what-is-agentproc' },
                { text: '快速开始', link: '/zh/guide/getting-started' },
              ],
            },
            {
              text: '协议规范',
              items: [
                { text: 'P0 协议规范', link: '/zh/spec/' },
              ],
            },
            {
              text: 'SDK',
              items: [
                { text: 'Python SDK', link: '/zh/sdk/python' },
                { text: 'Node.js SDK', link: '/zh/sdk/node' },
              ],
            },
            {
              text: '示例',
              items: [
                { text: '接入 claude CLI', link: '/zh/examples/claude' },
                { text: '裸脚本（无 SDK）', link: '/zh/examples/bare' },
              ],
            },
          ],
        },
      },
    },
  },

  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'AgentProc',

    nav: [
      { text: 'Quick Start', link: '/guide/getting-started' },
      { text: 'Specification', link: '/spec/' },
      { text: 'SDK', link: '/sdk/' },
      { text: 'Examples', link: '/examples/' },
    ],

    sidebar: {
      '/guide/': [
        {
          text: 'Introduction',
          items: [
            { text: 'What is AgentProc?', link: '/guide/what-is-agentproc' },
            { text: 'Quick Start', link: '/guide/getting-started' },
          ],
        },
      ],
      '/spec/': [
        {
          text: 'Specification',
          items: [
            { text: 'P0 Protocol', link: '/spec/' },
          ],
        },
      ],
      '/sdk/': [
        {
          text: 'SDK',
          items: [
            { text: 'Overview', link: '/sdk/' },
            { text: 'Python SDK', link: '/sdk/python' },
            { text: 'Node.js SDK', link: '/sdk/node' },
          ],
        },
      ],
      '/examples/': [
        {
          text: 'Examples',
          items: [
            { text: 'Connect claude CLI', link: '/examples/claude' },
            { text: 'Bare script (no SDK)', link: '/examples/bare' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/jeffkit/agentproc' },
    ],

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright © 2026 jeffkit',
    },

    search: {
      provider: 'local',
    },
  },
})
