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
        repoURL: () => 'https://github.com/asukaneko/vitepress', 
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
  title: "Neko bot",
  description: "A QQ bot by napcat",
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
      { text: '快速开始', link: '/guide/quick-start.md' }
    ],
    sidebar: {
      '/guide/': [
        {
          text: '快速上手',
          collapsed: false,
          items: [
            { text: '快速开始', link: '/guide/quick-start.md' },
            { text: '所有命令', link: '/guide/commands.md' },
          ]
        },
        {
          text: '项目开发',
          collapsed: true,
          items: [
            { text: '项目指南', link: '/guide/guide.md' },
            { text: 'bot.py', link: '/guide/page/bot.md'},
            { text: 'chat.py', link: '/guide/page/chat.md'},
            { text: 'commands.py', link: '/guide/page/commands.md'},
            { text: 'config.py', link: '/guide/page/config.md'},
            { text: 'update_novel.py', link: '/guide/page/update_novel.md'},
          ]
        },
        {
          text: '远程部署',
          collapsed: true,
          items: [
            { text: 'docker 部署指南', link: '/guide/docker-deploy.md' },
          ]
        },
        {
          text: 'napcat 开发',
          collapsed: true,
          items: [
            { text: '主页', link: '/napcat/index.md' },
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
            { text: 'api接口', link: '/napcat/api.md' },
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
      message: 'Released under the <a href="https://github.com/asukaneko/Ncatbot-comic-QQbot/blob/master/LICENSE">Apache 2.0 License</a>.',
      copyright: 'Copyright © 2025-present <a href="https://github.com/asukaneko">Asukaneko</a>'
    },
    editLink: {
      pattern: 'https://github.com/asukaneko/vitepress',
      text: '在GitHub上编辑此页'
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
