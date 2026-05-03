import { defineConfig } from 'vitepress'
import type { PluginOption } from 'vite'
import { groupIconMdPlugin, groupIconVitePlugin,localIconLoader } from 'vitepress-plugin-group-icons'
import { 
  GitChangelog, 
  GitChangelogMarkdownSection, 
} from '@nolebase/vitepress-plugin-git-changelog/vite'
import {
  PageProperties,
  PagePropertiesMarkdownSection,
} from '@nolebase/vitepress-plugin-page-properties/vite'

export default defineConfig({
  vite: { 
    optimizeDeps: {
      exclude: [ 
        '@nolebase/vitepress-plugin-enhanced-readabilities/client', 
        'vitepress', 
        '@nolebase/ui', 
      ], 
    },
    ssr: { 
      noExternal: [ 
        // 如果还有别的依赖需要添加的话，并排填写和配置到这里即可
        '@nolebase/vitepress-plugin-highlight-targeted-heading', 
        '@nolebase/vitepress-plugin-enhanced-readabilities', 
        '@nolebase/ui', 
      ], 
    }, 
    plugins: [
      PageProperties(),
      PagePropertiesMarkdownSection({
        excludes: ['index.md'],
      }),
      groupIconVitePlugin(
        { 
        customIcon: {
          ts: 'logos:typescript',
          js: 'logos:javascript', //js图标
          md: 'logos:markdown', //markdown图标
          css: 'logos:css-3', //css图标
          python:'logos:python',
          cpp:'logos:c-plus-plus',
          c:'logos:c'
        },
        }
      ), //代码组图标
      GitChangelog({ 
        // 填写在此处填写您的仓库链接
        repoURL: () => 'https://github.com/asukaneko/Ncatbot-comic-QQbot', 
      }), 
      GitChangelogMarkdownSection({
        sections: {
          // 禁用页面历史
          disableChangelog: false,
          // 禁用贡献者
          disableContributors: true,
        },
      }) as any,
    ],
  }, 
  title: "NekoBot",
  description: "多频道 AI 机器人 - QQ / Web / Telegram",
  lang: 'zh-CN',
  head: [['link', { rel: 'icon', href: '/neko.png' }]],
  themeConfig: {
    docFooter: { 
      prev: '上一页', 
      next: '下一页', 
    }, 
    outline: { 
      level: [2,4], // 显示2-4级标题
      label: '当前页大纲' // 文字显示
    },
    lastUpdated: {
      text: '最后更新于',
      formatOptions: {
        dateStyle: 'short', // 可选值full、long、medium、short
        timeStyle: 'medium' // 可选值full、long、medium、short
      },
    },
    logo:'/neko.png',
    nav: [
      { text: '主页', link: '/' },
      { text: '快速开始', link: '/guide/quick-start.md' },
      { text: '开发指南', link: '/guide/guide.md' },
      { 
        text: 'GitHub',
        link: 'https://github.com/asukaneko/Ncatbot-comic-QQbot'
      }
    ],
    sidebar: {
      '/guide/': [
        {
          text: '快速上手',
          collapsed: false,
          items: [
            { text: '快速开始', link: '/guide/quick-start.md' },
            { text: '所有命令', link: '/guide/commands.md' },
            { text: '更新日志', link: '/guide/changelog.md' },
          ]
        },
        {
          text: '项目开发',
          collapsed: true,
          items: [
            { text: '开发指南', link: '/guide/guide.md' },
            { text: '频道管理与接入', link: '/guide/channels.md' },
          ]
        },
        {
          text: 'nbot 核心模块',
          collapsed: true,
          items: [
            {
              text: 'core - AI核心',
              collapsed: true,
              items: [
                { text: 'ai_pipeline - 管道中间件', link: '/guide/nbot/core/ai_pipeline.md' },
                { text: 'chat_models - 聊天模型', link: '/guide/nbot/core/chat_models.md' },
                { text: 'agent_service - AI服务', link: '/guide/nbot/core/agent_service.md' },
                { text: 'session_store - 会话存储', link: '/guide/nbot/core/session_store.md' },
                { text: 'model_adapter - 模型适配', link: '/guide/nbot/core/model_adapter.md' },
                { text: 'workspace - 工作区', link: '/guide/nbot/core/workspace.md' },
                { text: 'workflow - 工作流', link: '/guide/nbot/core/workflow.md' },
              ]
            },
            {
              text: 'channels - 频道层',
              collapsed: true,
              items: [
                { text: 'add-channel - 新增频道', link: '/guide/nbot/channels/add-channel.md' },
                { text: 'base - 频道基类', link: '/guide/nbot/channels/base.md' },
                { text: 'registry - 频道注册', link: '/guide/nbot/channels/registry.md' },
                { text: 'qq - QQ适配器', link: '/guide/nbot/channels/qq.md' },
                { text: 'web - Web适配器', link: '/guide/nbot/channels/web.md' },
                { text: 'telegram - Telegram适配器', link: '/guide/nbot/channels/telegram.md' },
              ]
            },
            { 
              text: 'services - 服务层',
              collapsed: true,
              items: [
                { text: 'ai - AI客户端', link: '/guide/nbot/services/ai.md' },
                { text: 'tools - 工具系统', link: '/guide/nbot/services/tools.md' },
                { text: 'chat_service - 聊天服务', link: '/guide/nbot/services/chat_service.md' },
                { text: 'todo_tools - 待办工具', link: '/guide/nbot/services/todo_tools.md' },
              ]
            },
            { 
              text: 'plugins - 插件系统',
              collapsed: true,
              items: [
                { text: 'skills - 技能系统', link: '/guide/nbot/plugins/skills.md' },
                { text: 'dispatcher - 调度器', link: '/guide/nbot/plugins/dispatcher.md' },
              ]
            },
            { 
              text: 'web - Web后台',
              collapsed: true,
              items: [
                { text: 'server - 服务入口', link: '/guide/nbot/web/server.md' },
                { text: 'routes - API路由', link: '/guide/nbot/web/routes.md' },
              ]
            },
          ]
        },
        {
          text: '远程部署',
          collapsed: true,
          items: [
            { text: 'Docker 部署指南', link: '/guide/docker-deploy.md' },
          ]
        }
      ],
      '/napcat/':[
        {
          text:'快速上手',
          link:'/guide/quick-start.md'
        },
        {
          text:'基础',
          items: [
            { text: '主页', link: '/napcat/index.md' },
            { text: '接入框架', link: '/napcat/integration.md' },
            { text: '社区资源', link: '/napcat/community.md' },
          ]
        },
        {
          text:'协议',
          collapsed: true,
          items: [
            { text: 'API 接口', link: '/napcat/api.md' },
            { text: '事件基础结构', link: '/napcat/basic_event.md' },
            { text: '事件字段详情', link: '/napcat/event.md' },
            { text: '网络通讯', link: '/napcat/network.md' },
            { text: '消息元素定义', link: '/napcat/msg.md' },
          ]
        }
      ]
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/asukaneko/Ncatbot-comic-QQbot' }
    ],

    search: {
      provider: 'local'
    },
    footer: {
      message: 'Released under the <a href="https://github.com/asukaneko/Ncatbot-comic-QQbot/blob/main/LICENSE">MIT License</a>.',
      copyright: 'Copyright © 2025-present <a href="https://github.com/asukaneko">Asukaneko</a>'
    },
    editLink: {
      pattern: 'https://github.com/asukaneko/Ncatbot-comic-QQbot/edit/main/docs/docs/:path',
      text: '在 GitHub 上编辑此页'
    }
  },
  ignoreDeadLinks: true,
  markdown: {
    container: {
      tipLabel: '提示',
      warningLabel: '警告',
      dangerLabel: '危险',
      infoLabel: '信息',
      detailsLabel: '详细信息'
    },
    math: true,
    image: {
      // 开启图片懒加载
      lazyLoading: true
    },
    // 组件插入h1标题下
    config(md) {
      // 创建 markdown-it 插件
      md.use(groupIconMdPlugin) //代码组图标
      md.use((md) => {
        const defaultRender = md.render
        md.render = function (...args) {
          const [content, env] = args
          const isHomePage = env.path === '/' || env.relativePath === 'index.md'  // 判断是否是首页

          if (isHomePage) {
            return defaultRender.apply(md, args) // 如果是首页，直接渲染内容
          }
          // 在每个 md 文件内容的开头插入组件
          const defaultContent = defaultRender.apply(md, args)
          const component = '<ArticleMetadata />\n'
          return component + defaultContent
        }
      })
    }
  },
  lastUpdated: true
})
