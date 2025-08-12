# 用于更新轻小说的脚本，可以定期运行，自动更新小说内容
#这个代码用于更新轻小说文库的轻小说
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time, re, os, json
from bs4 import BeautifulSoup
import configparser

class NovelDownloader:
    def __init__(self):
        """初始化浏览器驱动"""
        # 配置浏览器选项
        self.edge_options = Options()
        # 初始化浏览器驱动
        self.service = Service('./edgedriver_win64/msedgedriver.exe')  # 替换为你的EdgeDriver路径
        self.driver = webdriver.Edge(service=self.service, options=self.edge_options)

        # 初始化数据存储
        self.names = []
        self.urls = []
        self.rest_names = []
        self.rest_urls = []

    def login(self):
        """登录轻小说文库最新入库网站"""
        self.driver.get("https://www.wenku8.net/modules/article/toplist.php?sort=postdate&page=1")

        # 等待并填写登录表单
        wait = WebDriverWait(self.driver, 5)
        email_field = wait.until(EC.presence_of_element_located((By.NAME, 'username')))
        password_field = wait.until(EC.presence_of_element_located((By.NAME, 'password')))

        email_field.send_keys("xxxxxxxxxx") # 替换为你的用户名
        password_field.send_keys("xxxxxxxxx") # 替换为你的密码

        # 点击登录按钮
        login_button = self.driver.find_element(By.NAME, 'submit')
        login_button.click()

        time.sleep(8)

    def get_novel_urls(self):
        """获取所有最新小说页面的URL"""
        self.purls = []
        for i in range(2, 194): # 替换为实际的页数范围,从第2页开始
            self.purls.append(f"https://www.wenku8.net/modules/article/articlelist.php?page={i}")


    def parse_page(self):
        """解析当前页面，获取小说信息"""
        res = self.driver.page_source
        bs = BeautifulSoup(res, 'html.parser')
        hrefs = bs.find_all('a')

        # 定义正则表达式模式
        pattern = r'/book/\d+\.htm'
        pa2 = r'tiptitle="[^"]+"'

        urls = []
        names = []

        # 提取小说信息
        for href in hrefs:
            href = str(href)
            url = re.findall(pattern, href)
            name = re.findall(pa2, href)

            if not url or not name or len(url[0]) == 0 or len(name[0]) == 0:
                continue

            names.append(name[0][10:-1])
            urls.append(url[0][6:-4])

        # 去重并保存
        urls = list(dict.fromkeys(urls))
        names = list(dict.fromkeys(names))

        for url, name in zip(urls, names):
            if 1:
                self.driver.get(f"https://www.wenku8.net/book/{url}.htm")
                res = self.driver.page_source
                soup = BeautifulSoup(res, 'html.parser')

                default_values = {
                    'category': '未知',
                    'author': '未知',
                    'status': '未知',
                    'last_update': '未知',
                    'length': '未知',
                    'introduction':'未知'
                }

                # 提取文库分类
                category_elem = soup.select_one('td:-soup-contains("文库分类：")')
                category = category_elem.text.replace('文库分类：', '').strip() if category_elem else default_values[
                    'category']

                # 提取小说作者
                author_elem = soup.select_one('td:-soup-contains("小说作者：")')
                author = author_elem.text.replace('小说作者：', '').strip() if author_elem else default_values['author']

                # 提取文章状态
                status_elem = soup.select_one('td:-soup-contains("文章状态：")')
                status = status_elem.text.replace('文章状态：', '').strip() if status_elem else default_values['status']

                # 提取最后更新日期
                last_update_elem = soup.select_one('td:-soup-contains("最后更新：")')
                last_update = last_update_elem.text.replace('最后更新：', '').strip() if last_update_elem else default_values['last_update']

                # 提取全文长度
                length_elem = soup.select_one('td:-soup-contains("全文长度：")')
                length = length_elem.text.replace('全文长度：', '').strip() if length_elem else default_values['length']
                
                # 提取简介
                introduction = soup.find('span', string='内容简介：').find_next('span').get_text(strip=True) if soup.find('span', string='内容简介：') else default_values['introduction']

                new = {
                    "res": url,
                    "author": author,
                    "category": category,
                    "last_date": last_update,
                    "word_count": length,
                    "introduction":introduction,
                    "download_url": f"https://dl.wenku8.com/down.php?type=txt&node=1&id={url}",
                    "is_serialize": status,
                    "page": f"https://www.wenku8.net/book/{url}.htm"
                }

                if name in data:
                    res = data[name]
                    if res == new:
                        continue

                data[name] = new
                print(f"已获取 {name} 的信息")
                with open('novel_details2.json', 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                    print(f"保存数据成功: {name}")
                time.sleep(0.5)


    def run(self):
        """主程序入口"""
        try:
            self.login()
            self.get_novel_urls()

            # 处理首页
            self.parse_page()

            # 处理其他页面
            for purl in self.purls:
                self.driver.get(purl)
                self.parse_page()
        finally:
            # 关闭浏览器
            self.driver.quit()

# 这里可能需要根据实际情况调整路径
data = {}
with open("novel_details2.json", "r", encoding="utf-8") as f:
    data = json.load(f)
downloader = NovelDownloader()
downloader.run()

