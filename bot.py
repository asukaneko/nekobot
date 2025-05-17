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
                if re.match(rf'^{re.escape(cmd)}(?:\s|$)', msg.raw_message):
                    await handler(msg, is_group=True)
                    return
        elif re.match(fr'^{re.escape(command)}(?:\s|$)', msg.raw_message): # 处理单个命令情况
            await handler(msg, is_group=False)
            return

    if msg.raw_message.startswith("/chat"):
        content = chat(msg.raw_message, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        await msg.reply(text=content)

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

        ori_content = "内容："
        try:
            ori_content += msg.message[2].get("data").get("text")  
        except IndexError:
            ori_content += "有人回复了你"

        reply_id = msg.message[0].get("data").get("id")
        msg_obj = await bot.api.get_msg(message_id=reply_id)
        print(msg_obj)
        if msg_obj.get("data").get("message")[0].get("type") == "image": #处理图片
            url = msg_obj.get("data").get("message")[0].get("data").get("url")
            content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,image=True,url=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
                await msg.reply(text=content)
            else:
                await msg.reply(text=content)
            return

        reply_text = "被回复的消息："+ msg_obj.get("data").get("raw_message") +", "
        
        content = chat(reply_text+ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
        else:
            await msg.reply(text=content)

@bot.private_event()
async def on_private_message(msg: PrivateMessage):
    global if_tts
    if_tts = commands.if_tts
    _log.info(msg)
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(rf'^{re.escape(cmd)}(?:\s|$)', msg.raw_message):
                    await handler(msg, is_group=False)
                    return
        elif re.match(fr'^{re.escape(command)}(?:\s|$)', msg.raw_message): # 处理单个命令情况
            await handler(msg, is_group=False)
            return
    
    try:
        if running[str(msg.user_id)]["state"]:
            running[str(msg.user_id)]["last_time"] = time.time()
        write_running()
    except KeyError:
        pass

    try:
        if msg.message[0].get("type") == "image": #处理图片
            url = msg.message[0].get("data").get("url")
            content = chat(user_id=msg.user_id,image=True,url=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.set_input_status(event_type=0,user_id=bot_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
                await bot.api.post_private_msg(msg.user_id, text=content)
            else:
                await bot.api.set_input_status(event_type=1,user_id=bot_id)
                await bot.api.post_private_msg(msg.user_id, text=content)
            return
    except IndexError:
        pass
    
    try:
        if msg.message[0].get("type") == "reply": #处理回复
            reply_id = msg.message[0].get("data").get("id")
            msg_obj = await bot.api.get_msg(message_id=reply_id)
            print(msg_obj)

            if msg_obj.get("data").get("message")[0].get("type") == "image": #处理图片
                url = msg_obj.get("data").get("message")[0].get("data").get("url")
                try:  #预防回复图片时没有内容的情况
                    content = chat(content=msg.message[1].get("data").get("text"),user_id=msg.user_id,image=True,url=url)
                except IndexError:
                    content = chat(user_id=msg.user_id,image=True,url=url)

                if if_tts:
                    rtf = tts(content)
                    await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
                    await bot.api.post_private_msg(msg.user_id, rtf=rtf)
                    await bot.api.post_private_msg(msg.user_id, text=content)
                else:
                    await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
                    await bot.api.post_private_msg(msg.user_id, text=content)
                return

            reply_text = "被回复的消息："+ msg_obj.get("data").get("raw_message") +", "
            try:
                ori_content = msg.message[1].get("data").get("text")
            except IndexError:
                ori_content = "回复了你"
            content = chat(reply_text+ori_content, user_id=msg.user_id)
            if if_tts:
                rtf = tts(content)
                await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
                await bot.api.post_private_msg(msg.user_id, text=content)
            else:
                await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, text=content)
            return
    except IndexError:
        pass
    
    if msg.raw_message and not msg.raw_message.startswith("/"): # 检查消息是否为空,避免接受文件后的空消息被回复
        content = chat(msg.raw_message, user_id=msg.user_id)
        if if_tts:
            rtf = tts(content)
            await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=content)
        else:
            await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, text=content)

if __name__ == "__main__":
    bot.run(reload=False)