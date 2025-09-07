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
![](https://img.shields.io/badge/LatestVersion-1.5.4-blue?&logo=react)

+ [CHANGLOG.md](https://github.com/asukaneko/NapCat-jmcomic_download-bot/blob/master/CHANGELOG.md)
---

## 📌Before You Start  
[![](https://img.shields.io/badge/python-version>=3.7-red?logo=python)]()
> **⚠️Notice**  
> If you encounter any error, please check if you have installed the latest version of napcat and ncatbot  
>
> If you are unable to install NapCat, please go to the [NapCat Download Page](https://github.com/NapNeko/NapCatQQ/releases/download/v4.8.95/NapCat.Shell.zip) to download the latest version of NapCat.Shell.zip, extract it to the root directory, and rename it to "napcat."
>**Do not expose the webui directly to the public network. If necessary, please set a strong password.**

>Environment: <u>___windows___</u>   
>Recommended to use a __secondary account__ for login  
>Developed based on jmcomic and ncatbot   
>For any issues, please submit to GitHub or email ycssbc@126.com  


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
/jm <comic_id> -> Download comics
/jmrank <monthly_rank/weekly_rank> -> Get rankings
/jm_clear -> Clear cache
/search <content> -> Search comics
/tag <tag> -> Search by tag
/add_black_list or /abl <comic_id> -> Add to blacklist
/del_black_list or /dbl <comic_id> -> Remove from blacklist
/list_black_list or /lbl -> View blacklist
/add_global_black_list or /agbl <comic_id> -> Add to global blacklist (admin)
/del_global_black_list or /dgbl <comic_id> -> Remove from global blacklist (admin)
/get_fav <username> <password> -> Get favorites (private chat only for groups)
/add_fav <comic_id> -> Add to favorites
/del_fav <comic_id> -> Remove from favorites
/list_fav -> View favorites list
/set_prompt or /sp <prompt> -> Set prompt
/del_prompt or /dp -> Delete prompt
/get_prompt or /gp -> Get prompt
/del_message or /dm -> Delete chat history
/active_chat <interval_hours> <enable(1/0)> -> Enable active chat
/show_chat or /sc -> Send complete chat history
/random_image or /ri -> Random image
/random_emoticons or /re -> Random emoticons
/st <tag> -> Send random NSFW image (tags support AND/OR (& |))
/random_video or /rv -> Random anime video
/random_dice or /rd -> Random dice
/random_rps or /rps -> Random rock-paper-scissors
/music <song_name/id> -> Send music
/random_music or /rm -> Random music
/dv <link> -> Download video
/di <link> -> Download image
/df <link> -> Download file
/mc <server_address> -> Check MC server status
/mc_bind <server_address> -> Bind MC server
/mc_unbind -> Unbind MC server
/mc_show -> View bound MC server
/generate_photo or /gf <description(no spaces)> <size> -> Generate image
/restart -> Restart bot (admin)
/tts -> Toggle TTS
/agree -> Accept friend request
/set_admin <qq_number> or /sa <qq_number> -> Set admin (root)
/del_admin <qq_number> or /da <qq_number> -> Remove admin (root)
/get_admin or /ga -> Get admin list
/set_ids <nickname> <signature> <gender> -> Set account info (admin)
/set_online_status <status> -> Set online status (admin)
/get_friends -> Get friend list (admin)
/set_qq_avatar <url> -> Change avatar (admin)
/send_like <target_qq> <times> -> Send likes
/bot.api.function_name(param1=value1,param2=value2) -> Custom API (admin), see `https://docs.ncatbot.xyz/guide/p8aun9nh/`
/shutdown -> Shutdown bot (admin)
/set_group_admin <target_qq> -> Set group admin (admin)
/del_group_admin <target_qq> -> Remove group admin (admin)
/findbook or /fb <book_title> -> Search and download light novel
/fa <author> -> Search by author
/select <number> -> Select novel to download
/info <book_title> -> Get novel info
/random_novel or /rn -> Random novel
/task </bot.api.xxxx(param1=value1...)> <hours> <loop(1/0)> -> Set timed task (admin)
/remind <hours> <content> -> Set reminder
/premind <MM-DD> <HH:MM> <content> -> Set precise reminder
/help or /h -> View help
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
+ Supports image recognition, video recognition
+ Supports web search. Go to https://opensearch.console.aliyun.com/cn-shanghai/rag/api-key  to obtain the API and domain address, which can be used for free.
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
