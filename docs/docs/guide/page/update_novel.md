# update_novel.py 文件文档

## 概述
`update_novel.py`是一个用于自动更新轻小说文库的Python脚本，使用Selenium和BeautifulSoup实现网页抓取和数据解析。

## 主要功能

### 1. NovelDownloader 类

#### 初始化
```python
def __init__(self):
    # 配置Edge浏览器选项
    # 初始化浏览器驱动
    # 初始化数据存储
```

#### 主要方法
- `login()`: 登录轻小说文库网站
- `get_novel_urls()`: 获取所有最新小说页面的URL
- `parse_page()`: 解析当前页面，获取小说详细信息
- `run()`: 主程序入口

### 2. 数据提取
从网页中提取以下小说信息：
- 小说名称
- 作者
- 文库分类
- 文章状态
- 最后更新日期
- 全文长度
- 内容简介
- 下载链接

### 3. 数据存储
将提取的小说信息保存到 `novel_details2.json` 文件中

## 代码结构
```python
class NovelDownloader:
    # 类实现

# 主程序
with open("novel_details2.json", "r", encoding="utf-8") as f:
    data = json.load(f)
    
downloader = NovelDownloader()
downloader.run()
```

## 依赖项
- Selenium: 用于浏览器自动化
- BeautifulSoup: 用于HTML解析
- Edge浏览器驱动: 需要配置正确的路径

## 使用说明
1. 需要先安装依赖: `pip install selenium beautifulsoup4`
2. 需要下载Edge浏览器驱动并配置正确路径
3. 在代码中填写正确的用户名和密码
4. 运行脚本会自动更新小说信息到 `novel_details2.json`

## 注意事项
- 需要稳定的网络连接
- 网站结构变化可能导致脚本失效
- 使用时应遵守网站的爬虫政策
        