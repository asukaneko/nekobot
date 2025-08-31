from ncatbot.utils.logger import get_log
import commands
from chat import chat,tts,chat_video,chat_image,chat_webpage,chat_json
from commands import *

_log = get_log()

if_tts = commands.if_tts

emotions = {}

def load_emotions():
    with open('emotions.json', 'r', encoding='utf-8') as f:
        global emotions
        emotions = json.load(f)

load_emotions()

def get_bilibili_real_url(short_url):
    """
    获取哔哩哔哩视频的真实URL。
    :param short_url: 哔哩哔哩视频的短链接。
    :return: 哔哩哔哩视频的真实URL。
    """
    try:
        response = requests.get(short_url, allow_redirects=False)
        if response.status_code == 302:
            return response.headers['Location']
        return short_url
    except:
        return short_url

def get_bilibili_video_url(url):
    """
    获取哔哩哔哩视频的URL。
    :param url: 哔哩哔哩视频的链接。
    :return: 哔哩哔哩视频的URL。
    """
    bvid = url.split("/video/")[1].split("/")[0].split("?")[0]
    cid_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    response = requests.get(cid_url,headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    })
    data = response.json()
    cid = data['data']['cid']
    api_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}"
    response = requests.get(api_url,headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    })
    data2 = response.json()
    video_url = data2['data']['durl'][0]['url']
    return video_url

def deal_forward(msg_obj) -> str:
    """
    处理转发消息。
    :param msg_obj: 转发消息对象。
    :return: 处理后的转发消息内容。
    """
    content = "这是一条转发消息，转发了以下内容：\n"
    tot = 0
    fg=1
    try:
        tp = msg_obj['type']
        if tp == "forward":
            fg=0
    except KeyError:
        fg=1
    for forward_msg in msg_obj['data']['message'][0]['data']['content'] if fg else msg_obj['data']['content']:
        # 获取消息类型
        msg_type = forward_msg['message'][0]['type']
        user = forward_msg['sender']['nickname']
        tot += 1
        content += f"第{tot}条，由{user}发送："
        if msg_type == "text":
            text = forward_msg['message'][0]['data']['text']
            content += text
        elif msg_type == "image":
            image_url = forward_msg['message'][0]['data']['url']
            content += "这是图片的描述："+chat_image(image_url)
        elif msg_type == "video":
            video_url = forward_msg['message'][0]['data']['url']
            content += "这是视频的描述："+chat_video(video_url)
        elif msg_type == "json":
            try:
                json_comtent = "这是一个QQ小程序，小程序的描述如下："
                json_data = forward_msg['message'][0]['data']['data']
                title = json.loads(json_data).get("meta", {}).get("detail_1", {}).get("title", "")
                desc = json.loads(json_data).get("meta", {}).get("detail_1", {}).get("desc", "")
                preview = json.loads(json_data).get("meta", {}).get("detail_1", {}).get("preview", "")
                image_describe = chat_image(preview)
                json_comtent += f"小程序的描述：{desc}\n小程序的标题：{title}\n小程序的预览图描述：{image_describe}\n"
                content += json_comtent
            except Exception:
                content += "这是一个QQ小程序，小程序的描述如下："+chat_json(str(forward_msg['message'][0]['data']['data']))
        elif msg_type == "forward":
            content += deal_forward(forward_msg['message'][0])    
        elif msg_type == "face":
            try:
                emo = emotions[forward_msg['message'][0].get('data').get('id')]
            except KeyError:
                emo = forward_msg['message'][0].get('data').get('raw').get('faceText')
                if not emo:
                    emo = ""
            content += f"发送了一个表情:{emo}"
        elif msg_type == "reply":
            try:
                if forward_msg['message'][1].get("type") == "text":
                    content += f"回复了一条消息：{forward_msg['message'][1].get('data').get('text')}"
                else:
                    content += f"回复了一条消息:{forward_msg['raw_message']}"
            except IndexError:
                content += "回复了一条消息"
        else:
            content += "这是一条"+str(msg_type)+"消息"
        content += "\n"
    return content

@bot.group_event()
async def on_group_message(msg: GroupMessage):
    global if_tts
    if_tts = commands.if_tts
    _log.info(msg)
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message):
                    await handler(msg, is_group=True)
                    _log.info(f"调用{cmd}命令")
                    return
        elif re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message): # 处理单个命令情况
            await handler(msg, is_group=False)
            _log.info(f"调用{command}命令")
            return

    if msg.raw_message.startswith("/chat"):
        content = chat(msg.raw_message, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        _log.info("调用chat命令")
        await msg.reply(text=content)
    
    if msg.message[0].get("type") == "at" and msg.message[0].get("data").get("qq") == bot_id:
    #如果是at机器人
        try:
            if msg.message[1].get("type") == "text":
                ori_content = msg.message[1].get("data").get("text") #避免@的消息为空
            elif msg.message[1].get("type") == "face":
                try:
                    emo = emotions[msg.message[1].get('data').get('id')]
                except KeyError:
                    emo = msg.message[1].get('data').get('raw').get('faceText')
                    if emo:
                        emo = f"[表情:{emo}]"
                    else:
                        emo = ""
                ori_content = f"发送了一个表情:{emo}"
        except IndexError:
            ori_content = f"用户{msg.user_id}@了你"
        
        content = chat(ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
        await msg.reply(text=content)

    if msg.message[0].get("type") == "reply" and msg.message[1].get("type") == "at" and msg.message[1].get("data").get("qq") == bot_id:
        #如果是回复机器人的消息
        ori_content = ""
        try:
            ori_content += msg.message[2].get("data").get("text")  
        except IndexError:
            ori_content += "有人回复了你"

        reply_id = msg.message[0].get("data").get("id")
        msg_obj = await bot.api.get_msg(message_id=reply_id)
        if msg_obj.get("data").get("message")[0].get("type") == "image": #处理图片
            url = msg_obj.get("data").get("message")[0].get("data").get("url")
            content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,image=True,url=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
            return
        
        if msg_obj.get("data").get("message")[0].get("type") == "video": #处理视频
            url = msg_obj.get("data").get("message")[0].get("data").get("url")
            content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,video=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
            return

        if msg_obj.get("data").get("message")[0].get("type") == "json":
            try:
                json_data = msg_obj.get("data").get("message")[0].get("data").get("data")
                title = json.loads(json_data).get("meta").get("detail_1").get("title", "")
                desc = json.loads(json_data).get("meta").get("detail_1").get("desc", "")
                preview = json.loads(json_data).get("meta").get("detail_1").get("preview", "")
            except Exception:
                content = "发送了一个QQ小程序分享:"+chat_json(str(msg_obj.get("data").get("message")[0].get("data").get("data")))
                res = chat(group_id=msg.group_id,group_user_id=msg.sender.nickname,content=content)
                if if_tts:
                    rtf = tts(res)
                    await bot.api.post_group_msg(msg.group_id, rtf=rtf)
                await msg.reply(text=res)
                return
            if not preview.startswith("http"):
                preview = "https://"+preview
            content = f"发送了一个QQ小程序分享:\n标题: {title}\n描述: {desc}。{ori_content}"
            if "哔哩哔哩" in title:
                url = json.loads(json_data).get("meta", {}).get("detail_1", {}).get("qqdocurl", "")
                url = get_bilibili_real_url(url)
                res = chat_webpage(url)
                content = content+"视频内容:"+res
            
            res = chat(group_id=msg.group_id,group_user_id=msg.sender.nickname,content=content,image=True,url=preview)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=res)
            return

        if msg_obj.get("data").get("message")[0].get("type") == "forward":
            msg_forward_obj = await bot.api.get_msg(message_id=reply_id)
            content = deal_forward(msg_forward_obj)
            res = chat(group_id=msg.group_id,group_user_id=msg.sender.nickname,content=ori_content+content)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=res)
            return
        
        if msg_obj.get("data").get("message")[0].get("type") == "face":
            try:
                emo = emotions[msg_obj.get('data').get('message')[0].get('data').get('id')]
            except KeyError:
                emo = msg_obj.get('data').get('message')[0].get('data').get('raw').get('faceText')
                if not emo:
                    emo = ""
            content = f"发送了一个表情:{emo}"
            ori_content = ori_content+content
            res = chat(group_id=msg.group_id,group_user_id=msg.sender.nickname,content=ori_content)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=res)
            return

        content = chat(reply_text+ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
        await msg.reply(text=content)

@bot.private_event()
async def on_private_message(msg: PrivateMessage):
    global if_tts
    if_tts = commands.if_tts
    _log.info(msg)
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message):
                    _log.info(f"调用{cmd}命令")
                    await handler(msg, is_group=False)
                    return
        elif re.match(fr'^{re.escape(command)}(?:\s|\.|$)', msg.raw_message): # 处理单个命令情况
            _log.info(f"调用{command}命令")
            await handler(msg, is_group=False)
            return
    
    try:
        if running[str(msg.user_id)]["state"]:
            running[str(msg.user_id)]["last_time"] = time.time()
        write_running()
    except KeyError:
        pass
    
    try:
        if msg.message == []:
            return
        msg.message[0]
    except Exception as e:
        pass

    # 处理QQ小程序消息
    if msg.message[0].get("type") == "json":
        try:
            json_data = msg.message[0].get("data").get("data")
            title = json.loads(json_data).get("meta").get("detail_1").get("title", "")
            desc = json.loads(json_data).get("meta").get("detail_1").get("desc", "")
            preview = json.loads(json_data).get("meta").get("detail_1").get("preview", "")
            if not preview.startswith("http"):
                preview = "https://"+preview
            content = f"发送了一个QQ小程序分享:\n标题: {title}\n描述: {desc}"
            if "哔哩哔哩" in title:
                url = json.loads(json_data).get("meta").get("detail_1").get("qqdocurl", "")
                url = get_bilibili_real_url(url)
                try:
                    video_content = chat_webpage(url)
                except Exception as e:
                    video_content = ""
                    _log.error(f"处理b站视频出错: {e}")
                content = content+"\n视频描述: "+video_content
            
            res = chat(user_id=msg.user_id,content=content,image=True,url=preview)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=res)
            return
        except Exception as e:
            _log.info(f"处理QQ小程序分享出错: {e}，切换为普通文本")
            content = "发送了一个QQ小程序分享:"+chat_json(str(msg.message[0].get("data").get("data")))
            res = chat(user_id=msg.user_id,content=content)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=res)
            return

    if msg.message[0].get("type") == "video": #处理视频
        url = msg.message[0].get("data").get("url")
        content = chat(user_id=msg.user_id,video=url)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        await bot.api.post_private_msg(msg.user_id, text=content)
        return

    try:
        if msg.message[0].get("type") == "image": #处理图片
            url = msg.message[0].get("data").get("url")
            content = chat(user_id=msg.user_id,image=True,url=url)
            if if_tts:
                rtf = tts(content)
                await bot.api.set_input_status(event_type=0,user_id=bot_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
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
                else:
                    await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, text=content)
                return

            reply_text = "这是被回复的消息："+ msg_obj.get("data").get("raw_message") +"。 "
            try:
                ori_content = msg.message[1].get("data").get("text")
            except IndexError:
                ori_content = "回复了你"
            content = chat(reply_text+ori_content, user_id=msg.user_id)
            if if_tts:
                rtf = tts(content)
                await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            else:
                await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, text=content)
            return
    except IndexError:
        pass
    
    if msg.message[0].get("type") == "forward": # 处理转发消息
        msg_obj = await bot.api.get_msg(message_id=msg.message_id)
        print(msg_obj)
        res = deal_forward(msg_obj)
        content = chat(res, user_id=msg.user_id)
        if if_tts:
            rtf = tts(content)
            await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        else:
            await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text=content)
        return
    
    if msg.message[0].get("type") == "face": # 处理表情
        try:
            emo = emotions[msg.message[0].get("data").get("id")]
        except KeyError:
            emo = msg.message[0].get("data").get('raw').get('faceText')
            if not emo:
                emo = ""
        res = f"发送了一个表情:{emo}"
        content = chat(res, user_id=msg.user_id)
        if if_tts:
            rtf = tts(content)
            await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        else:
            await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text=content)
        return

    if msg.raw_message and not msg.raw_message.startswith("/"): # 检查消息是否为空,避免接受文件后的空消息被回复
        content = chat(msg.raw_message, user_id=msg.user_id)
        if if_tts:
            rtf = tts(content)
            await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        else:
            await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text=content)

async def handle_command(msg, command_handlers):
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message):
                    _log.info(f"调用{cmd}命令")
                    await handler(msg, is_group=False)
                    return
        elif re.match(fr'^{re.escape(command)}(?:\s|\.|$)', msg.raw_message): # 处理单个命令情况
            _log.info(f"调用{command}命令")
            await handler(msg, is_group=False)
            return

if __name__ == "__main__":
    import threading
    import asyncio
    # 启动机器人
    bot_thread = threading.Thread(target=bot.run, kwargs={'enable_webui_interaction': False})
    bot_thread.start()
    loop = asyncio.new_event_loop()
    _log.info("命令行模式已启动，可以在命令行内输入命令")
    while True:
        try:
            command = input("").strip()
            if command.lower() == 'exit':
                _log.info("已退出命令行模式")
                break
            message = {
                "raw_message":command,
                "user_id":admin_id
            }
            msg = GroupMessage(message)
            loop.run_until_complete(handle_command(msg, command_handlers))
            time.sleep(1)
        except KeyboardInterrupt:
            _log.info("退出命令输入")
            break