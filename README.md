<div align="center">
<h1 style = "text-align:center;">🚀NapCat Comic Downloader & AI Chatbot</h1>
<h1 style = "text-align:center;">🚀可以下载本子和聊天的猫娘机器人</h1>

![cover](https://img.picui.cn/free/2025/04/19/6803c76d2bbf9.png)

> _a bot for downloading comics and chatting with ai_

</div>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
![](https://visitor-badge.laobi.icu/badge?page_id=asukaneko.NapCat-jmcomic_download-bot)
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

---
### ✨Updates 更新![](https://img.shields.io/badge/LatestVersion-1.2.3-blue?&logo=react)
+ [2025/4/27] v1.2.3 新增命令   
    - 新增群聊管理分支
    - 新增命令：
        - 设置群管理员：`/set_group_admin <qq号>`
        - 取消群管理员：`/del_group_admin <qq号>`
        - 将漫画添加到黑名单: `/add_black_list <漫画id>`
        - 将漫画从黑名单移除: `/del_black_list <漫画id>`
        - 将漫画添加到全局黑名单: `/add_global_black_list <漫画id>`
        - 将漫画从全局黑名单移除: `/del_global_black_list <漫画id>`
        - 查看当前群聊或用户的黑名单: `/list_black_list`


+ [2025/4/24 - 2025/4/25] v1.2.2 更新
    - 新增命令：
        - 搜索音乐并发送：`/music <歌曲名/id>`
        - 暂只支持网易云音乐
    - 聊天可支持回复过往的内容
    - 优化回复逻辑，对图片有更好的处理

+ [2025/4/22] v1.2.1 优化
    - 优化/help命令，采用分级菜单展示，同时规范了help_text格式
    - 优化命令匹配方式，更改为正则匹配，更加精准

+ [2025/4/21] v1.2.0 新增权限功能和定时提醒功能
    - 新增命令：
        - 设置权限：`/sa <qq号>`
        - 权限列表：`/ga`
        - 取消权限：`/da <qq号>`
        - 定时提醒：`/remind <时间(hour)> <消息>`
        - 日期定时体现：`/premind YYYY-MM-DD HH:MM 内容`
    - 将原有部分命令调整为仅管理员可用
    - 超级管理员(root)为config.ini中root的值


+ [2025/4/19] v1.1.0 原有基础上增加了收藏夹功能
    - 新增命令：
        - 收藏：`/add_fav`
        - 收藏夹：`/list_fav`
        - 取消收藏：`/del_fav`

---

### 📌Before You Start / 开始之前![](https://img.shields.io/badge/GitHub-Before_start-blue?logo=github)  
[![](https://img.shields.io/badge/python-version>=3.7-red?logo=python)]()
>Environment: <u>___windows___</u>  
>环境为 <u>___windows___</u>  
>Recommended to use a __secondary account__ for login  
>建议使用 __小号__ 登录  
>Developed based on jmcomic and ncatbot  
>基于jmcomic和ncatbot开发  
>For any issues, please submit to GitHub or email ycssbc@126.com  
>有任何问题欢迎提issue，或发送给我的邮箱ycssbc@126.com  

 **📝Related Documents / 相关文档**

| jmcomic | [![](https://img.shields.io/badge/jmcomic-on_Github-blue)](https://github.com/hect0x7/JMComic-Crawler-Python) | [![](https://img.shields.io/badge/jmcomic-Readthedocs.io-orange)](https://jmcomic.readthedocs.io/zh-cn/latest/) |
|:-:|:-:|:-:|

| napcat |[![](https://img.shields.io/badge/napcat-on_Github-blue)](https://github.com/NapNeko/NapCatQQ) | [![](https://img.shields.io/badge/napcat-Github.IO-orange)](https://napneko.github.io)
|:-:|:-:|:-:|

| ncatbot | [![](https://img.shields.io/badge/ncatbot-on_Github-blue)](https://github.com/liyihao1110/ncatbot) | [![](https://img.shields.io/badge/Python_Sdk-Ncatbot-8A2BE2)](https://docs.ncatbot.xyz/) |
|:-:|:-:|:-:|

### 📂Directory Structure
```
.
│  bot.py
│  chat.py
│  config.ini
│  config.py
│  commands.py
│  LICENSE
│  neko.txt
│  number.txt
│  option.yml
│  README.md
│  requirements.txt
│  urls.ini
│  
├─logs
│      
├─napcat
│  ├─...... 
│  └─......
├─plugins
│
├─prompts
│  ├─group
│  └─user
├─saved_images
│      
├─saved_message
│
└─cache
    ├─comic
    ├─saved_images
    ├─list
    ├─fav
    ├─pdf
    └─......
```

### ⬇️Download Source Code / 下载源码 [![](https://img.shields.io/badge/GitHub-Download_Source_Code-blue?logo=github)](https://github.com/asukaneko/NapCat-jmcomic_download-bot.git)
```
git clone https://github.com/asukaneko/NapCat-jmcomic_download-bot.git
```

### 📦Install Dependencies / 安装依赖![](https://img.shields.io/badge/GitHub-Install_Dependencies-blue?logo=github)
```
cd NapCat-jmcomic_download-bot
pip install -r requirements.txt
```

### ⚙️Configuration / 更改配置![](https://img.shields.io/badge/GitHub-Configuration-blue?logo=github)
```
config.ini: Modify account, AI API, image saving settings
config.ini：修改账号、大模型api、图片保存等

option.yml: Modify comic download directory, download method
option.yml: 修改漫画下载目录、下载方式等

urls.ini: (Optional) Modify image API URLs, default URLs will be used if not modified
urls.ini:(可不修改) 修改图片获取api地址, 如不修改则使用默认地址
```

### ▶️Run / 运行 ![](https://img.shields.io/badge/GitHub-Run-red?logo=github)
```
python bot.py
```
The console will prompt to download napcat, then scan QR code to login  
命令行会提示下载napcat，然后扫码登录即可

### 📜Commands / 命令相关 ![](https://img.shields.io/badge/GitHub-Commands-yellow?logo=github)
>__commands.py contains ___command registration decorators___, you can add custom commands__  

>__commands.py 中 设置了 ___命令注册装饰器___ ，可以自行添加命令__  
Command List:
```text
/jm xxxxxx Download comics  
/jm xxxxxx 下载漫画  
/search xxx Search comics  
/search xxx 搜索漫画  
/get_fav Get favorites  
/get_fav 获取收藏夹  
/jmrank Get rankings  
/jmrank 获取排行榜  
/add_fav xxx Add to favorites  
/add_fav xxx 添加收藏夹  
/set_prompt or /sp Set prompt  
/set_prompt 或 /sp 设置提示词  
/del_prompt or /dp Delete prompt  
/del_prompt 或 /dp 删除提示词  
/get_prompt or /gp Get prompt  
/get_prompt 或 /gp 获取提示词  
/agree   Accept friend request  
/agree   同意好友请求  
/restart   Restart Bot  
/restart   重启Bot  
/random_image or /ri Send random image  
/random_image 或 /ri 发送随机图片  
/random_emoticons or /re Send random emoticons  
/random_emoticons 或 /re 发送随机表情包  
/st tag Send random NSFW image, tags support AND/OR (& |)  
/st 标签名 发送随机涩图,标签支持与或(& |)  
/help or /h View help  
/help 或 /h 查看帮助 
...
```

### 💡Tips / 提示 ![](https://img.shields.io/badge/GitHub-Tips-green?logo=github)
+ After login, you can find webui login URL in napcat\logs folder
+ 登录以后可在napcat\logs文件夹下找到webui的登录地址
+ ___If napcat is not properly configured, open: http://localhost:6099, default token is 'napcat', then login and create a websocket server in network configuration with default port 3001___
+ ___如果你发现没有正常配置napcat，则打开网址：http://localhost:6099, token默认为napcat，然后登录，在网络配置那里新建一个websocket服务端，端口为默认的3001，然后保存即可___
+ Use _/chat_ command or _@bot_ in group chat to start conversation, private chat handles all non-command messages as conversation by default
+ 群聊使用 _/chat_ 命令或 _@机器人_ 即可聊天，私聊默认处理所有非命令消息为聊天
+ _Default AI model is from SiliconFlow, new users get 15CNY free credit_
+ _大模型默认使用硅基流动的，新用户免费赠送15元_
+ For AI to process images in group chat, first send image then reply to your own message mentioning the bot
+ 群聊想要ai处理图片，则先发图片，再回复自己的信息，回复内容必须要先@机器人
+ TTS supports custom voice upload, see comments in chat.py for details
+ tts可以自己上传音色，详情看chat.py中注释
+ Modify prompts in neko.txt to create different characters
+ 可以更改neko.txt中的提示词，实现不同的角色

### 🌟More Features / 更多 ![](https://img.shields.io/badge/GitHub-More_Features-blue?logo=github)
+ Supports image recognition, get API from https://platform.moonshot.cn/console/account (15CNY free credit), add to config.ini (second API)
+ 支持图片识别，需去https://platform.moonshot.cn/console/account 获取api，免费赠送15元。填入config.ini中（第二个api）
+ Supports multi-group chat and custom user prompts
+ 支持多群聊以及用户自定义提示词
+ Group chat supports user awareness, chat supports time awareness
+ 群聊支持用户感知，聊天支持时间感知
+ Supports conversation history saving
+ 支持保存对话记录
+ Quick command adding
+ 快速添加命令
+ Low configuration requirements, lightweight, small memory footprint
+ 配置要求低，轻量，占用内存小

[your-project-path]:asukaneko/NapCat-jmcomic_download-bot
[contributors-shield]: https://img.shields.io/github/contributors/asukaneko/NapCat-jmcomic_download-bot.svg?style=flat
[contributors-url]: https://github.com/asukaneko/NapCat-jmcomic_download-bot/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/asukaneko/NapCat-jmcomic_download-bot.svg?style=flat
[forks-url]: https://github.com/asukaneko/NapCat-jmcomic_download-bot/network/members
[stars-shield]: https://img.shields.io/github/stars/asukaneko/NapCat-jmcomic_download-bot.svg?style=flat
[stars-url]: https://github.com/asukaneko/NapCat-jmcomic_download-bot/stargazers
[issues-shield]: https://img.shields.io/github/issues/asukaneko/NapCat-jmcomic_download-bot.svg?style=flat
[issues-url]: https://img.shields.io/github/issues/asukaneko/NapCat-jmcomic_download-bot.svg
[license-shield]: https://img.shields.io/github/license/asukaneko/NapCat-jmcomic_download-bot.svg?style=flat
[license-url]: https://github.com/asukaneko/NapCat-jmcomic_download-bot/blob/master/LICENSE
[linkedin-shield]: https://img.shields.io/badge/-LinkedIn-black.svg?style=flat&logo=linkedin&colorB=555
