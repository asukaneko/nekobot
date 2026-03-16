import DefaultTheme from 'vitepress/theme'
import { h ,type Plugin } from 'vue'
import './styles/index.css'
import myLayout from './components/mylayout.vue'
import 'virtual:group-icons.css' //代码组样式
import Linkcard from "./components/Linkcard.vue"
import confetti from "./components/confetti.vue"
import ArticleMetadata from "./components/ArticleMetadata.vue"
import giscusTalk from 'vitepress-plugin-comment-with-giscus';
import { useData, useRoute } from 'vitepress';
import mediumZoom from 'medium-zoom';
import { onMounted, watch, nextTick } from 'vue';
import BackToTopButton from '@miletorix/vitepress-back-to-top-button' 
import '@miletorix/vitepress-back-to-top-button/style.css' 
import { 
  NolebaseEnhancedReadabilitiesMenu, 
  NolebaseEnhancedReadabilitiesScreenMenu, 
} from '@nolebase/vitepress-plugin-enhanced-readabilities/client'
import '@nolebase/vitepress-plugin-enhanced-readabilities/client/style.css'
import { NolebaseEnhancedReadabilitiesPlugin } from '@nolebase/vitepress-plugin-enhanced-readabilities/client'
import type { Options } from '@nolebase/vitepress-plugin-enhanced-readabilities/client'
import { 
  NolebaseGitChangelogPlugin 
} from '@nolebase/vitepress-plugin-git-changelog/client'
import '@nolebase/vitepress-plugin-git-changelog/client/style.css'
import {
  NolebaseHighlightTargetedHeading,
} from '@nolebase/vitepress-plugin-highlight-targeted-heading/client'
import '@nolebase/vitepress-plugin-highlight-targeted-heading/client/style.css'
import { NProgress } from 'nprogress-v2/dist/index.js' // 进度条组件
import 'nprogress-v2/dist/index.css' // 进度条样式
import { NolebasePagePropertiesPlugin } from '@nolebase/vitepress-plugin-page-properties'
import '@nolebase/vitepress-plugin-page-properties/client/style.css'
import darkchangeLayout from './components/darkchangeLayout.vue'

let homePageStyle: HTMLStyleElement | undefined

export default {
  extends: DefaultTheme,
  Layout: () => {
    return h(DefaultTheme.Layout, null, {
      default: () => h(darkchangeLayout),
      // 为较宽的屏幕的导航栏添加阅读增强菜单
      'nav-bar-content-after': () => [
        h(NolebaseEnhancedReadabilitiesMenu)
      ], 
      // 为较窄的屏幕（通常是小于 iPad Mini）添加阅读增强菜单
      'nav-screen-content-after': () => h(NolebaseEnhancedReadabilitiesScreenMenu), 
      'layout-top': () => [ 
        h(NolebaseHighlightTargetedHeading), 
      ], 
    })
  },
  enhanceApp({app,router}) {
    app.use(NolebaseGitChangelogPlugin)
    app.use(
      NolebasePagePropertiesPlugin<{
        progress: number
      }>() as Plugin,
      {
        properties: {
          'zh-CN': [
            {
              key: 'wordCount',
              type: 'dynamic',
              title: '字数',
              options: {
                type: 'wordsCount',
              },
            },
            {
              key: 'readingTime',
              type: 'dynamic',
              title: '阅读时间',
              options: {
                type: 'readingTime',
                dateFnsLocaleName: 'zhCN',
              },
            },
            {
              key: 'updatedAt',
              type: 'datetime',
              title: '更新时间',
              formatAsFrom: true,
              dateFnsLocaleName: 'zhCN',
            },
          ],
        },
      },
    )
    app.use(NolebaseEnhancedReadabilitiesPlugin, {  
      spotlight: {
        defaultToggle: true,
      },  
    } as Options)
    if (typeof window !== 'undefined') {
      watch(
        () => router.route.data.relativePath,
        () => updateHomePageStyle(location.pathname === '/'),
        { immediate: true },
      )
    } 
    BackToTopButton(app.app,{
      progressColor:'#CE9FFC'
    }) 
    // 注册全局组件
    app.component('Linkcard' , Linkcard)
    app.component('confetti' , confetti)
    //app.component('ArticleMetadata' , ArticleMetadata) 已有替代
    
    if (typeof document !== 'undefined')  {
      NProgress.configure({ showSpinner: false })
      router.onBeforeRouteChange = () => {
        NProgress.start() // 开始进度条
      }
      router.onAfterRouteChanged = () => {
        NProgress.done() // 停止进度条
      }
    }
  },
  setup() {   
    // Get frontmatter and route
    const { frontmatter } = useData();
    const route = useRoute();
        
    // giscus配置
    giscusTalk({
      repo: 'asukaneko/vitepress', //仓库
      repoId: 'R_kgDOPrMWKg', //仓库ID
      category: 'General', // 讨论分类
      categoryId: 'DIC_kwDOPrMWKs4CvFPT', //讨论分类ID
      mapping: 'pathname',
      inputPosition: 'bottom',
      lang: 'zh-CN',
    }, 
    {
      frontmatter, route
    },
    //默认值为true，表示已启用，此参数可以忽略；
    //如果为false，则表示未启用
    //您可以使用“comment:true”序言在页面上单独启用它
    true
    );

    const initZoom = () => {
      // mediumZoom('[data-zoomable]', { background: 'var(--vp-c-bg)' }); // 默认
      mediumZoom('.main img', { background: 'var(--vp-c-bg)' }); // 不显式添加{data-zoomable}的情况下为所有图像启用此功能
    };
    onMounted(() => {
      initZoom();
    });
    watch(
      () => route.path,
      () => nextTick(() => initZoom())
    );
  }
}

function updateHomePageStyle(value: boolean) {
  if (value) {
    if (homePageStyle) return

    homePageStyle = document.createElement('style')
    homePageStyle.innerHTML = `
    :root {
      animation: rainbow 12s linear infinite;
    }`
    document.body.appendChild(homePageStyle)
  } else {
    if (!homePageStyle) return
 
    homePageStyle.remove()
    homePageStyle = undefined
  }
}
