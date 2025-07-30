<div align="center">
<h1 style = "text-align:center;">🚀A Comic Downloader & AI Chatbot for QQ</h1>
</div>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
![](https://visitor-badge.laobi.icu/badge?page_id=asukaneko.NapCat-jmcomic_download-bot)
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

---
[中文版 | Chinese](https://github.com/asukaneko/NapCat-jmcomic_download-bot/blob/master/Chinese.md)
## ✨Updates  
![](https://img.shields.io/badge/LatestVersion-1.4.3-blue?&logo=react)

+ [CHANGLOG.md](https://github.com/asukaneko/NapCat-jmcomic_download-bot/blob/master/CHANGELOG.md)
---

## 📌Before You Start  
[![](https://img.shields.io/badge/python-version>=3.7-red?logo=python)]()
> **⚠️Notice**  
> if you encounter any error, please check if you have installed the latest version of napcat and ncatbot  
>
> If you are unable to install NapCat, please go to the [NapCat Download Page](https://github.com/NapNeko/NapCatQQ/releases/download/v4.8.95/NapCat.Shell.zip) to download the latest version of NapCat.Shell.zip, extract it to the root directory, and rename it to "napcat."


>Environment: <u>___windows___</u>   
>Recommended to use a __secondary account__ for login  
>Developed based on jmcomic and ncatbot   
>For any issues, please submit to GitHub or email ycssbc@126.com  


 **📝Related Documents**

docker：https://asukablog.iepose.cn/archives/5f583afd-c9b1-420f-bc4b-41f4dfc039d3  
| jmcomic | [![](https://img.shields.io/badge/jmcomic-on_Github-blue)](https://github.com/hect0x7/JMComic-Crawler-Python) | [![](https://img.shields.io/badge/jmcomic-Readthedocs.io-orange)](https://jmcomic.readthedocs.io/zh-cn/latest/) |
|:-:|:-:|:-:|

| napcat |[![](https://img.shields.io/badge/napcat-on_Github-blue)](https://github.com/NapNeko/NapCatQQ) | [![](https://img.shields.io/badge/napcat-Github.IO-orange)](https://napneko.github.io)
|:-:|:-:|:-:|

| ncatbot | [![](https://img.shields.io/badge/ncatbot-on_Github-blue)](https://github.com/liyihao1110/ncatbot) | [![](https://img.shields.io/badge/Python_Sdk-Ncatbot-8A2BE2)](https://docs.ncatbot.xyz/) |
|:-:|:-:|:-:|


## =============== ↓ Quick Start  ↓ ===============
### ⬇️Download Source Code  
```
git clone https://github.com/asukaneko/Ncatbot-comic-QQbot.git
```

### 📦Install Dependencies   
```
cd Ncatbot-comic-QQbot
pip install -r requirements.txt
```

### ⚙️Configuration 
```
config.ini: Modify account, AI API, image saving settings

option.yml: Modify comic download directory, download method

urls.ini: (Optional) Modify image API URLs, default URLs will be used if not modified
```

### ▶️Run 
```
python bot.py
```
The console will prompt to download napcat, then scan QR code to login  

### 📜Commands 
>__commands.py contains ___command registration decorators___, you can add custom commands__  
Command List:
```text
/jm xxxxxx Download comics  
/search xxx Search comics  
/get_fav Get favorites   
/jmrank Get rankings  
/add_fav xxx Add to favorites  
/set_prompt or /sp Set prompt  
/del_prompt or /dp Delete prompt  
/get_prompt or /gp Get prompt   
/agree   Accept friend request    
/restart   Restart Bot  
/random_image or /ri Send random image    
/random_emoticons or /re Send random emoticons   
/st tag Send random NSFW image, tags support AND/OR (& |)   
/help or /h View help  
...
```

## 💡Tips 
+ After login, you can find webui login URL in napcat\logs folder
+ ___If napcat is not properly configured, open: http://localhost:6099, default token is 'napcat', then login and create a websocket server in network configuration with default port 3001___
+ Use _/chat_ command or _@bot_ in group chat to start conversation, private chat handles all non-command messages as conversation by default
+ _Default AI model is from SiliconFlow, new users get 15CNY free credit_
+ For AI to process images in group chat, first send image then reply to your own message mentioning the bot
+ TTS supports custom voice upload, see comments in chat.py for details
+ Modify prompts in neko.txt to create different characters

## 🌟More Features
+ Supports image recognition, get API from https://platform.moonshot.cn/console/account (15CNY free credit), add to config.ini (second API)
+ Supports multi-group chat and custom user prompts
+ Group chat supports user awareness, chat supports time awareness
+ Supports conversation history saving
+ Quick command adding
+ Low configuration requirements, lightweight, small memory footprint

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
