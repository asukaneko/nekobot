from ncatbot.utils.logger import get_log
import commands
from chat import chat,tts
from commands import *

_log = get_log()

if_tts = commands.if_tts

@bot.group_event()
async def on_group_message(msg: GroupMessage):
    global if_tts
    if_tts = commands.if_tts
    _log.info(msg)
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(f'^{re.escape(cmd)}(?:\s|$)', msg.raw_message):
                    await handler(msg, is_group=True)
                    return
        elif re.match(f'^{re.escape(command)}(?:\s|$)', msg.raw_message): # 处理单个命令情况
            await handler(msg, is_group=False)
            return

    if msg.raw_message.startswith("/chat"):
        content = chat(msg.raw_message, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        await msg.reply(text=content)

    if msg.message[0].get("type") == "image": #提取保存所有图片的信息
        url = msg.message[0].get("data").get("url")
        if str(msg.group_id) not in group_imgs:  # 添加检查并初始化
            group_imgs[str(msg.group_id)] = []
        group_imgs[str(msg.group_id)].append({str(msg.message_id):url})

    if msg.message[0].get("type") == "at" and msg.message[0].get("data").get("qq") == bot_id:
    #如果是at机器人
        try:
            ori_content = msg.message[1].get("data").get("text") #避免@的消息为空
        except IndexError:
            ori_content = f"用户{msg.user_id}@了你"
        content = chat(ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
        else:
            await msg.reply(text=content)

    if msg.message[0].get("type") == "reply" and msg.message[1].get("type") == "at" and msg.message[1].get("data").get("qq") == bot_id:
        #如果是回复机器人的消息
        get_id = msg.message[0].get("data").get("id") #判断是不是图片
        try:
            dirs = group_imgs[str(msg.group_id)]
        except KeyError:
            dirs = []
        for ldir in dirs:
            if ldir.get(get_id):
                url = ldir.get(get_id)
                ori_content = ""
                try:
                    ori_content = msg.message[2].get("data").get("text")
                except IndexError:
                    pass
                content = chat(ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,image=True,url=url)
                if if_tts:
                    rtf = tts(content)
                    await bot.api.post_group_msg(msg.group_id, rtf=rtf)
                    await msg.reply(text=content)
                else:
                    await msg.reply(text=content)
                return
        try:
            ori_content = msg.message[2].get("data").get("text")   #这里使用的是回复的消息，看不了被回复的消息
        except IndexError:
            ori_content = "有人回复了你"
        content = chat(ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
        else:
            await msg.reply(text=content)

running = True #用于定时聊天的开关

@bot.private_event()
async def on_private_message(msg: PrivateMessage):
    global if_tts
    if_tts = commands.if_tts
    _log.info(msg)
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(f'^{re.escape(cmd)}(?:\s|$)', msg.raw_message):
                    await handler(msg, is_group=False)
                    return
        elif re.match(f'^{re.escape(command)}(?:\s|$)', msg.raw_message): # 处理单个命令情况
            await handler(msg, is_group=False)
            return
    """
    if msg.raw_message.startswith("/开始定时聊天"):
        global running
        running = True
        async def chat_loop():
            while running:
                try:
                    next_time = random.random() * 10  # 1-2小时
                    await schedule_task(next_time, chatter, msg)
                    await asyncio.sleep(next_time)
                except Exception as e:
                    _log.error(f"定时聊天出错: {e}")
                    await asyncio.sleep(10)  # 出错后等待10秒
        asyncio.create_task(chat_loop())
        await bot.api.post_private_msg(msg.user_id, text="已开始定时聊天喵~")
    elif msg.raw_message.startswith("/结束定时聊天"):
        running = False
        await bot.api.post_private_msg(msg.user_id, text="已停止定时聊天喵~")
    """
    try:
        if msg.message[0].get("type") == "image": #处理图片
            url = msg.message[0].get("data").get("url")
            content = chat(user_id=msg.user_id,image=True,url=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
                await bot.api.post_private_msg(msg.user_id, text=content)
            else:
                await bot.api.post_private_msg(msg.user_id, text=content)
            return
    except IndexError:
        pass

    if msg.raw_message and not msg.raw_message.startswith("/"): # 检查消息是否为空,避免接受文件后的空消息被回复
        content = chat(msg.raw_message, user_id=msg.user_id)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=content)
        else:
            await bot.api.post_private_msg(msg.user_id, text=content)

if __name__ == "__main__":
    bot.run(reload=False)