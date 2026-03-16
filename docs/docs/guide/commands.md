# 命令手册

## 漫画相关

| 命令 | 说明 |
|------|------|
| `/jm <ID>` | 下载漫画 |
| `/jmrank <月排行/周排行>` | 获取排行榜 |
| `/jm_clear` | 清除缓存 |
| `/search <内容>` | 搜索漫画 |
| `/tag <标签>` | 搜索漫画标签 |

## 收藏管理

| 命令 | 说明 |
|------|------|
| `/get_fav <用户> <密码>` | 获取收藏夹 |
| `/add_fav <ID>` | 添加收藏 |
| `/del_fav <ID>` | 删除收藏 |
| `/list_fav` | 查看收藏列表 |

## 黑名单

| 命令 | 说明 |
|------|------|
| `/add_black_list` 或 `/abl <ID>` | 添加黑名单 |
| `/del_black_list` 或 `/dbl <ID>` | 删除黑名单 |
| `/list_black_list` 或 `/lbl` | 查看黑名单 |
| `/add_global_black_list` 或 `/agbl <ID>` | 全局黑名单 (管理员) |
| `/del_global_black_list` 或 `/dgbl <ID>` | 删除全局黑名单 (管理员) |

## AI 聊天

| 命令 | 说明 |
|------|------|
| `/set_prompt` 或 `/sp <提示词>` | 设置提示词 |
| `/del_prompt` 或 `/dp` | 删除提示词 |
| `/get_prompt` 或 `/gp` | 获取提示词 |
| `/del_message` 或 `/dm` | 删除对话记录 |
| `/主动聊天` | 切换主动聊天 |
| `/show_chat` 或 `/sc` | 发送完整聊天记录 |
| `/summary_today` | 总结今天聊天内容 |
| `/summary_recent` 或 `/sr [数量]` | 总结最近消息 |
| `/auto_reply [话痨程度]` | 开启/关闭自动回复 |

## 娱乐功能

| 命令 | 说明 |
|------|------|
| `/random_image` 或 `/ri` | 随机图片 |
| `/random_emoticons` 或 `/re` | 随机表情包 |
| `/st <标签>` | 随机涩图 |
| `/random_video` 或 `/rv` | 随机视频 |
| `/random_dice` 或 `/rd` | 随机骰子 |
| `/random_rps` 或 `/rps` | 石头剪刀布 |
| `/music <歌曲名>` | 发送音乐 |
| `/random_music` 或 `/rm` | 随机音乐 |
| `/generate_photo` 或 `/gf <描述>` | AI 生成图片 |
| `/识别人物` | 识别图片人物 |

## 下载功能

| 命令 | 说明 |
|------|------|
| `/dv <链接>` | 下载视频 |
| `/di <链接>` | 下载图片 |
| `/df <链接>` | 下载文件 |

## 轻小说

| 命令 | 说明 |
|------|------|
| `/findbook` 或 `/fb <书名>` | 搜索轻小说 |
| `/fa <作者>` | 搜索作者 |
| `/select <编号>` | 选择下载 |
| `/info <书名>` | 获取信息 |
| `/random_novel` 或 `/rn` | 随机小说 |
| `/hotnovel` | 热门轻小说 |

## MC 服务器

| 命令 | 说明 |
|------|------|
| `/mc <地址>` | 查询服务器 |
| `/mc_bind <地址>` | 绑定服务器 |
| `/mc_unbind` | 解绑服务器 |
| `/mc_show` | 查看服务器 |

## 定时任务

| 命令 | 说明 |
|------|------|
| `/remind <小时> <内容>` | 定时提醒 |
| `/premind <日期> <时间> <内容>` | 精确时间提醒 |
| `/task <命令> <时间> <循环>` | 定时任务 |
| `/list_tasks` 或 `/lt` | 查看任务 |
| `/cancel_tasks` 或 `/ct <名称>` | 取消任务 |

## 系统管理

| 命令 | 说明 |
|------|------|
| `/restart` | 重启机器人 |
| `/shutdown` | 关闭机器人 |
| `/tts` | 开关 TTS |
| `/agree` | 同意好友请求 |
| `/at_all` | @全体成员 |

## 管理员

| 命令 | 说明 |
|------|------|
| `/set_admin` 或 `/sa <QQ>` | 设置管理员 |
| `/del_admin` 或 `/da <QQ>` | 删除管理员 |
| `/get_admin` 或 `/ga` | 管理员列表 |
| `/set_group_admin <QQ>` | 群管理员 |
| `/del_group_admin <QQ>` | 取消群管理员 |
| `/set_ids <昵称> <签名> <性别>` | 设置账号信息 |
| `/set_online_status <状态>` | 在线状态 |
| `/get_friends` | 好友列表 |
| `/set_qq_avatar <地址>` | 更改头像 |
| `/send_like <QQ> <次数>` | 发送点赞 |
| `/bot.api.xxx()` | 自定义 API |

## 其他

| 命令 | 说明 |
|------|------|
| `/help` 或 `/h` | 帮助 |
| `/translate` 或 `/tr <文本>` | 翻译 |
| `/fortune` 或 `/jrrp` | 今日运势 |

---

::: tip
所有命令定义在 `nbot/commands.py` 中，使用 `@register_command` 装饰器注册。
:::
