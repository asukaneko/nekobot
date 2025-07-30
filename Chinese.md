<div align="center">
<h1 style = "text-align:center;">🚀可以下载本子和聊天的QQ猫娘机器人</h1>

![cover](https://img.picui.cn/free/2025/04/19/6803c76d2bbf9.png)

> _a catgirl QQ bot for downloading comics and chatting with ai_

</div>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
![](https://visitor-badge.laobi.icu/badge?page_id=asukaneko.NapCat-jmcomic_download-bot)
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

---
## ✨Updates 更新  
![](https://img.shields.io/badge/LatestVersion-1.5.0-blue?&logo=react)

+ 详细查看[CHANGLOG.md](https://github.com/asukaneko/NapCat-jmcomic_download-bot/blob/master/CHANGELOG.md)
---

## 📌开始之前  
[![](https://img.shields.io/badge/python-version>=3.7-red?logo=python)]()
>**⚠️重要提示**  
> 若出现未知错误，请检查是否已安装最新版本的napcat和ncatbot  
>
> 若在命令行阶段无法安装napcat，请去[napcat下载页](https://github.com/NapNeko/NapCatQQ/releases/download/v4.8.95/NapCat.Shell.zip)下载最新版的NapCat.Shell.zip，解压到根目录，并把文件夹重命名为napcat

>环境为 <u>___windows___</u>  
>建议使用 __小号__ 登录  
>基于jmcomic和ncatbot开发  
>有任何问题欢迎提issue，或发送给我的邮箱ycssbc@126.com  

 **📝相关文档**

docker部署指南：https://asukanas.iepose.cn/archives/5f583afd-c9b1-420f-bc4b-41f4dfc039d3
| jmcomic | [![](https://img.shields.io/badge/jmcomic-on_Github-blue)](https://github.com/hect0x7/JMComic-Crawler-Python) | [![](https://img.shields.io/badge/jmcomic-Readthedocs.io-orange)](https://jmcomic.readthedocs.io/zh-cn/latest/) |
|:-:|:-:|:-:|

| napcat |[![](https://img.shields.io/badge/napcat-on_Github-blue)](https://github.com/NapNeko/NapCatQQ) | [![](https://img.shields.io/badge/napcat-Github.IO-orange)](https://napneko.github.io)
|:-:|:-:|:-:|

| ncatbot | [![](https://img.shields.io/badge/ncatbot-on_Github-blue)](https://github.com/liyihao1110/ncatbot) | [![](https://img.shields.io/badge/Python_Sdk-Ncatbot-8A2BE2)](https://docs.ncatbot.xyz/) |
|:-:|:-:|:-:|


## =============== ↓ 快速开始 ↓ ===============
### ⬇️下载源码 
```
git clone https://github.com/asukaneko/Ncatbot-comic-QQbot.git
```

### 📦安装依赖  
```
cd Ncatbot-comic-QQbot
pip install -r requirements.txt
```

### ⚙️更改配置
```
config.ini：修改账号、大模型api、图片保存等

option.yml: 修改漫画下载目录、下载方式等

urls.ini:(可不修改) 修改图片获取api地址, 如不修改则使用默认地址
```

### ▶️运行 
```
python bot.py
```
命令行会提示下载napcat，然后扫码登录即可

### 📜命令相关 
>__commands.py 中 设置了 ___命令注册装饰器___ ，可以自行添加命令__  
Command List:
```text 
/jm xxxxxx 下载漫画   
/search xxx 搜索漫画  
/get_fav 获取收藏夹   
/jmrank 获取排行榜  
/add_fav xxx 添加收藏夹  
/set_prompt 或 /sp 设置提示词  
/del_prompt 或 /dp 删除提示词  
/get_prompt 或 /gp 获取提示词  
/agree   同意好友请求  
/restart   重启Bot  
/random_image 或 /ri 发送随机图片  
/random_emoticons 或 /re 发送随机表情包  
/st 标签名 发送随机涩图,标签支持与或(& |)  
/help 或 /h 查看帮助 
...
```

## 💡提示 
+ 登录以后可在napcat\logs文件夹下找到webui的登录地址
+ ___如果你发现没有正常配置napcat，则打开网址：http://localhost:6099, token默认为napcat，然后登录，在网络配置那里新建一个websocket服务端，端口为默认的3001，然后保存即可___
+ 群聊使用 _/chat_ 命令或 _@机器人_ 即可聊天，私聊默认处理所有非命令消息为聊天
+ _大模型默认使用硅基流动的，新用户免费赠送15元_
+ 群聊想要ai处理图片，则先发图片，再回复自己的信息，回复内容必须要先@机器人
+ tts可以自己上传音色，详情看chat.py中注释
+ 可以更改neko.txt中的提示词，实现不同的角色

## 🌟更多 
+ 支持图片识别，需去https://platform.moonshot.cn/console/account 获取api，免费赠送15元。填入config.ini中（第二个api）
+ 支持联网搜索，去https://opensearch.console.aliyun.com/cn-shanghai/rag/api-key获取api和域名地址，可以免费使用
+ 支持多群聊以及用户自定义提示词
+ 群聊支持用户感知，聊天支持时间感知
+ 支持保存对话记录
+ 快速添加命令
+ 配置要求低，轻量，占用内存小

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
