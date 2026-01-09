from ncatbot.utils.logger import get_log
import commands
from chat import chat,tts,chat_video,chat_image,chat_webpage,chat_json,record_assistant_message,record_user_message,log_to_group_full_file,judge_reply,load_prompt,chat_gif
from commands import *
import os
import json
import datetime
import asyncio

_log = get_log()

if_tts = commands.if_tts

emotions = {}

async def safe_set_input_status(event_type, user_id):
    try:
        await bot.api.set_input_status(event_type=event_type, user_id=user_id)
    except Exception:
        pass

private_text_buffers = {}

def load_emotions():
    with open('emotions.json', 'r', encoding='utf-8') as f:
        global emotions
        emotions = json.load(f)

load_emotions()

def extract_group_plain_text(msg):
    try:
        segments = msg.message
    except Exception:
        return getattr(msg, "raw_message", "")
    if not segments:
        return getattr(msg, "raw_message", "")
    parts = []
    for seg in segments:
        t = seg.get("type")
        d = seg.get("data", {})
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            qq = d.get("qq")
            if qq == "all":
                parts.append("@全体成员")
            else:
                parts.append(f"@{qq}")
        elif t == "face":
            try:
                emo = emotions[d.get("id")]
            except Exception:
                raw = d.get("raw") or {}
                emo = raw.get("faceText") or ""
            if emo:
                parts.append(f"[表情:{emo}]")
        elif t == "image":
            parts.append("[图片]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "json":
            parts.append("[小程序]")
        elif t == "reply":
            parts.append("[回复消息]")
        elif t == "forward":
            parts.append("[转发消息]")
        else:
            if t:
                parts.append(f"[{t}]")
    if parts:
        return "".join(parts)
    return getattr(msg, "raw_message", "")

def safe_parse_chat_response(content):
    """
    安全解析 chat 函数返回的 JSON 字符串。
    如果解析成功，只返回 msg 字段的内容。
    如果解析失败，则将 content 作为普通文本回复。
    """
    try:
        if not content:
            return "", []
        temp_content = content.strip()
        if temp_content.startswith("```json"):
            temp_content = temp_content[7:]
            if temp_content.endswith("```"):
                temp_content = temp_content[:-3]
            content = temp_content.strip()
        elif temp_content.startswith("```"):
            temp_content = temp_content[3:]
            if temp_content.endswith("```"):
                temp_content = temp_content[:-3]
            content = temp_content.strip()
        s = content
        first = s.find("{")
        last = s.rfind("}")
        if first != -1 and last != -1 and first < last:
            s = s[first:last+1]
        try:
            from json_repair import repair_json
            s = repair_json(s)
        except Exception:
            pass
        res_json = json.loads(s)
        if isinstance(res_json, dict):
            # 只有当确实包含 msg 字段时才认为是有效的 JSON 协议格式
            if "msg" in res_json:
                msg_content = res_json.get("msg", "")
                cmds = res_json.get("cmd", [])
                if not isinstance(cmds, list):
                    cmds = []
                else:
                    cmds = [c for c in cmds if isinstance(c, str)]
                return msg_content, cmds
            # 如果是 dict 但没有 msg 字段，可能是普通的包含大括号的内容，回退到原样返回
            else:
                return content, []
    except Exception:
        pass
    return content, []

commands.safe_parse_chat_response = safe_parse_chat_response

async def log_group_message(msg):
    # 记录消息收到的时刻
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        content = extract_group_plain_text(msg)
    except Exception:
        content = getattr(msg, "raw_message", "")
    
    group_id = str(getattr(msg, "group_id", ""))
    user_id = str(getattr(msg, "user_id", ""))
    nickname = ""
    try:
        nickname = msg.sender.nickname
    except Exception:
        nickname = ""

    # 检查是否有图片需要解析
    try:
        segments = getattr(msg, "message", []) or []
        url = None
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image":
                url = (seg.get("data") or {}).get("url")
                if url:
                    break
        
        if url:
            # 异步调用模型解析图片，不阻塞其他任务
            desc = await asyncio.to_thread(chat_image, url)
            if desc:
                content += f" [图片内容: {desc}]"
    except Exception as e:
        try:
            _log.error(f"解析群聊图片内容失败: {e}")
        except Exception:
            pass

    log_to_group_full_file(group_id, user_id, nickname, content, timestamp=timestamp)

async def _flush_user_text_buffer(user_id):
    buf = private_text_buffers.get(user_id)
    if not buf:
        return
    texts = buf.get("texts") or []
    msg = buf.get("last_msg")
    if_tts_local = buf.get("if_tts", False)
    private_text_buffers.pop(user_id, None)
    if not msg or not texts:
        return
    merged_text = "\n".join(texts)
    await _handle_plain_text_message(msg, merged_text, if_tts_local)

async def _schedule_flush_user_text_buffer(user_id):
    await asyncio.sleep(1.0)   # 缓冲区时间，1秒内的消息会合并发送
    await _flush_user_text_buffer(user_id)

async def _handle_plain_text_message(msg, raw_text, if_tts_local):
    try:
        content = chat(raw_text, user_id=msg.user_id)
    except Exception as e:
        try:
            _log.error(f"chat error: {e}")
        except Exception:
            pass
        return
    content, cmds = safe_parse_chat_response(content)
    if if_tts_local:
        rtf = tts(content)
        await safe_set_input_status(event_type=0,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, rtf=rtf)
    else:
        await safe_set_input_status(event_type=1,user_id=msg.user_id)
    await bot.api.post_private_msg(msg.user_id, text=content)
    if cmds:
        for cmd in cmds:
            message = {
            "raw_message":cmd,
            "user_id":str(msg.user_id)
            }
            msg2 = PrivateMessage(message)
            await handle_command(msg2, is_group=False)
            time.sleep(1)

async def get_recent_group_messages(group_id, count=20):
    """获取最近的群聊消息作为上下文"""
    try:
        history = await bot.api.get_group_msg_history(group_id, message_seq=0, count=count, reverse_order=True)
        items = []
        if isinstance(history, list):
            items = history
        elif isinstance(history, dict):
            data = history.get("data")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                msgs = data.get("messages")
                if isinstance(msgs, list):
                    items = msgs
        
        lines = []
        for item in items:
            nickname = ""
            user_id = ""
            text = ""
            if isinstance(item, dict):
                user_id = item.get("user_id", "")
                sender = item.get("sender", {})
                if isinstance(sender, dict):
                    nickname = sender.get("nickname", "")
                
                # 尝试从 message 数组中提取文本
                msg_segments = item.get("message", [])
                if isinstance(msg_segments, list):
                    # 模拟一个消息对象来调用 extract_group_plain_text
                    class DummyMsg:
                        def __init__(self, message):
                            self.message = message
                    
                    text = extract_group_plain_text(DummyMsg(msg_segments))
                else:
                    text = item.get("raw_message", "")
            
            name = nickname or str(user_id)
            if name and text:
                lines.append(f"{name}: {text}")
            elif text:
                lines.append(text)
        
        return "\n".join(lines)
    except Exception as e:
        _log.error(f"获取最近消息失败: {e}")
        return ""

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

def recognize_image(iurl):
    """
    识别二次元人物。
    :param url: 图片的URL。
    :return: 二次元人物的名称。
    """
    try:
        url = "https://dio.jite.me/api/recognize"
        pic = requests.get(iurl)
        with open("image.jpg", "wb") as f:
            f.write(pic.content)
        with open("image.jpg", "rb") as f:
            files = {"file": ("image.jpg", f, "image/jpeg")}
            data = {"use_correction": "1"}
            resp = requests.post(url, files=files, data=data, timeout=30)
            print(resp.status_code)
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        os.remove("image.jpg")
        return "这是来自"+resp.json()['faces'][0]['anime']+"的"+resp.json()['faces'][0]['name']+"，识别结果的置信度为"+str(resp.json()['faces'][0]['score'])

    except Exception as e:
        _log.error(f"识别失败: {e}")
        os.remove("image.jpg")
        return "识别失败"

@bot.group_event()
async def on_group_message(msg: GroupMessage):
    if str(msg.user_id) == str(bot_id):
        return
    _log.info(msg)
    try:
        asyncio.create_task(log_group_message(msg))
    except Exception:
        pass
    if_tts = switch.get_switch_state('tts', group_id=str(msg.group_id))
    if msg.raw_message.startswith("/command"):
        if str(msg.user_id) in admin:
            command = msg.raw_message.split(" ")[1].lower()
            if command == "on":
                switch.set_switch_state('command', True, group_id=str(msg.group_id))
                await msg.reply(text="已开启命令功能")
            elif command == "off":
                switch.set_switch_state('command', False, group_id=str(msg.group_id))
                await msg.reply(text="已关闭命令功能")
            else:
                await msg.reply(text="未知命令")

    if switch.get_switch_state('command', group_id=str(msg.group_id)):
        ok = await handle_command(msg, is_group=True)
        if(ok):
            return

    
    is_at_bot = msg.message[0].get("type") == "at" and msg.message[0].get("data").get("qq") == bot_id
    is_at_all = msg.message[0].get("type") == "at" and msg.message[0].get("data").get("qq") == 'all' and str(msg.group_id) in at_all_group
    
    if is_at_bot or is_at_all:
    #如果是at机器人或者at全体成员并且该群开启了识别@全体成员功能
        try:
            if len(msg.message) > 1 and msg.message[1].get("type") == "text":
                ori_content = ""
                for i in range(1,len(msg.message)):
                    if msg.message[i].get("type") == "text":
                        ori_content += msg.message[i].get("data").get("text")
    
            elif len(msg.message) > 1 and msg.message[1].get("type") == "face":
                try:
                    emo = emotions[msg.message[1].get('data').get('id')]
                except KeyError:
                    emo = msg.message[1].get('data').get('raw').get('faceText')
                    if emo:
                        emo = f"[表情:{emo}]"
                    else:
                        emo = ""
                ori_content = f"发送了一个表情:{emo}"
            else:
                ori_content = f"用户{msg.user_id}@了你"
        except (IndexError, AttributeError):
            ori_content = f"用户{msg.user_id}@了你"
        
        if is_at_all:
            recent_msgs = await get_recent_group_messages(msg.group_id, 20)
            if recent_msgs:
                ori_content = f"【群聊上下文记录】\n{recent_msgs}\n\n【当前消息】\n{ori_content}\n\n请根据以上上下文记录，理解并回复当前这条@全体成员的消息。"

        content = chat(ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        content, cmds = safe_parse_chat_response(content)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
        await msg.reply(text=content)
        if cmds:
            for cmd in cmds:
                message = {
                "raw_message":cmd,
                "group_id":str(msg.group_id),
                "user_id":str(msg.user_id)
                }
                msg2 = GroupMessage(message)
                await handle_command(msg2, is_group=True)
                time.sleep(1)

    if msg.message[0].get("type") == "reply" and msg.message[1].get("type") == "at" and msg.message[1].get("data").get("qq") == bot_id:
        #如果是回复机器人的消息
        ori_content = ""
        try:
            for i in range(2,len(msg.message)):
                if msg.message[i].get("type") == "text":
                    ori_content += msg.message[i].get("data").get("text")
        except IndexError:
            ori_content += "有人回复了你："

        reply_id = msg.message[0].get("data").get("id")
        msg_obj = await bot.api.get_msg(message_id=reply_id)

        if msg_obj.get("data").get("message")[0].get("type") == "image": #处理图片
            url = msg_obj.get("data").get("message")[0].get("data").get("url")
            summary = msg_obj.get("data").get("message")[0].get("data").get("summary")
            is_animated = summary == "[动画表情]"
            if is_animated:
                ori_content += "[发送了一个动画表情]"

            if "/识别人物" in ori_content.strip():
                _log.info("识别人物中...")
                ori_content += f"(识别结果：{recognize_image(url)})"
                content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,image=True,url=url)
            else:
                if is_animated:
                    desc = chat_gif(url)
                    content = chat(content=f"{ori_content} 动画多帧识别结果:{desc}",group_id=msg.group_id,group_user_id=msg.sender.nickname)
                else:
                    content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,image=True,url=url)
            content, _ = safe_parse_chat_response(content)
            if if_tts:
                rtf = tts(content)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=content)
            return
        
        if msg_obj.get("data").get("message")[0].get("type") == "video": #处理视频
            url = msg_obj.get("data").get("message")[0].get("data").get("url")
            content = chat(content=ori_content,group_id=msg.group_id,group_user_id=msg.sender.nickname,video=url)
            content, _ = safe_parse_chat_response(content)
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
                res, _ = safe_parse_chat_response(res)
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
            res, _ = safe_parse_chat_response(res)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=res)
            return

        if msg_obj.get("data").get("message")[0].get("type") == "forward":
            msg_forward_obj = await bot.api.get_msg(message_id=reply_id)
            content = deal_forward(msg_forward_obj)
            res = chat(group_id=msg.group_id,group_user_id=msg.sender.nickname,content=ori_content+content)
            res, _ = safe_parse_chat_response(res)
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
            res, _ = safe_parse_chat_response(res)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_group_msg(msg.group_id, rtf=rtf)
            await msg.reply(text=res)
            return

        content = chat(reply_text+ori_content, group_id=msg.group_id,group_user_id=msg.sender.nickname)
        content, cmds = safe_parse_chat_response(content)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_group_msg(msg.group_id, rtf=rtf)
        await msg.reply(text=content)
        if cmds:
            for cmd in cmds:
                message = {
                "raw_message":cmd,
                "group_id":str(msg.group_id),
                "user_id":str(msg.user_id)
                }
                msg2 = GroupMessage(message)
                await handle_command(msg2, is_group=True)
                time.sleep(1) 

    try:
        if not msg.message:
            return
        first_seg = msg.message[0]
    except Exception:
        return

    if not switch.get_switch_state('auto_reply', group_id=str(msg.group_id)):
        return

    if first_seg.get("type") != "text":
        return

    try:
        plain_text = extract_group_plain_text(msg)
    except Exception:
        plain_text = msg.raw_message

    if not plain_text:
        return

    try:
        recent_msgs = await get_recent_group_messages(msg.group_id, 20)
    except Exception as e:
        _log.error(f"auto_reply get_recent_group_messages error: {e}")
        recent_msgs = ""

    try:
        persona = load_prompt(group_id=msg.group_id)
    except Exception:
        persona = ""

    judge_text = plain_text
    if recent_msgs:
        judge_text = f"【最近群聊记录】\n{recent_msgs}\n\n【当前消息】\n{plain_text}"
    if persona:
        judge_text = f"【机器人人设与行为说明】\n{persona}\n\n{judge_text}"

    try:
        reply_score = await asyncio.to_thread(judge_reply, judge_text)
    except Exception as e:
        _log.error(f"auto_reply judge error: {e}")
        return

    try:
        level = float(switch.group_switches.get(str(msg.group_id), {}).get('auto_reply_level', 0.5))
    except Exception:
        level = 0.5

    if reply_score < level:
        return

    content = await asyncio.to_thread(
        chat,
        plain_text,
        None,
        msg.group_id,
        msg.sender.nickname,
        False,
        None,
        None,
    )
    content, cmds = safe_parse_chat_response(content)
    if if_tts:
        rtf = tts(content)
        await bot.api.post_group_msg(msg.group_id, rtf=rtf)
    await msg.reply(text=content)
    if cmds:
        for cmd in cmds:
            message = {
            "raw_message":cmd,
            "group_id":str(msg.group_id),
            "user_id":str(msg.user_id)
            }
            msg2 = GroupMessage(message)
            await handle_command(msg2, is_group=True)
            time.sleep(1)

@bot.private_event()
async def on_private_message(msg: PrivateMessage):
    if str(msg.user_id) == str(bot_id):
        return
    _log.info(msg)
    
    # 更新主动聊天的时间
    try:
        commands.update_user_active_chat_time(msg.user_id)
    except Exception:
        pass
        
    if_tts = switch.get_switch_state('tts', user_id=str(msg.user_id))
    if msg.raw_message.startswith("/command"):
        command = msg.raw_message.split(" ")[1].lower()
        if command == "on":
            switch.set_switch_state('command', True, user_id=str(msg.user_id))
            await msg.reply(text="已开启命令功能")
        elif command == "off":
            switch.set_switch_state('command', False, user_id=str(msg.user_id))
            await msg.reply(text="已关闭命令功能")
        else:
            await msg.reply(text="未知命令")
                
    if switch.get_switch_state('command', user_id=str(msg.user_id)):
        ok = await handle_command(msg, is_group=False)
        if ok:
            return

    # 此处原来的更新逻辑已移除，改用上方统一调用
    
    try:
        if msg.message == []:
            return
        msg.message[0]
    except Exception as e:
        pass

    if msg.raw_message and not msg.raw_message.startswith("/"):
        try:
            if msg.message and msg.message[0].get("type") == "text":
                user_id_str = str(msg.user_id)
                buf = private_text_buffers.get(user_id_str)
                if not buf:
                    private_text_buffers[user_id_str] = {
                        "texts": [msg.raw_message],
                        "last_msg": msg,
                        "if_tts": if_tts,
                    }
                    private_text_buffers[user_id_str]["task"] = asyncio.create_task(
                        _schedule_flush_user_text_buffer(user_id_str)
                    )
                else:
                    buf["texts"].append(msg.raw_message)
                    buf["last_msg"] = msg
                    buf["if_tts"] = if_tts
                return
        except Exception:
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
            res, _ = safe_parse_chat_response(res)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=res)
            return
        except Exception as e:
            _log.info(f"处理QQ小程序分享出错: {e}，切换为普通文本")
            content = "发送了一个QQ小程序分享:"+chat_json(str(msg.message[0].get("data").get("data")))
            res = chat(user_id=msg.user_id,content=content)
            res, _ = safe_parse_chat_response(res)
            if if_tts:
                rtf = tts(res)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            await bot.api.post_private_msg(msg.user_id, text=res)
            return

    if msg.message[0].get("type") == "video": #处理视频
        url = msg.message[0].get("data").get("url")
        content = chat(user_id=msg.user_id,video=url)
        content, _ = safe_parse_chat_response(content)
        if if_tts:
            rtf = tts(content)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        await bot.api.post_private_msg(msg.user_id, text=content)
        return

    try:
        if msg.message[0].get("type") == "image": #处理图片
            url = msg.message[0].get("data").get("url")
            summary = msg.message[0].get("data").get("summary")
            if summary == "[动画表情]":
                desc = chat_gif(url)
                content = chat(user_id=msg.user_id,content="发送了一个动画表情 动画多帧识别结果:"+desc)
            else:
                content = chat(user_id=msg.user_id,image=True,url=url)
            content, _ = safe_parse_chat_response(content)
            if if_tts:
                rtf = tts(content)
                await safe_set_input_status(event_type=0,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            else:
                await safe_set_input_status(event_type=1,user_id=msg.user_id)
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
                    text = msg.message[1].get("data").get("text")
                    res = ""
                    if "/识别人物" in text:
                        _log.info("识别人物中...")
                        res = f"(识别结果：{recognize_image(url)})"
                    content = chat(content=msg.message[1].get("data").get("text")+res,user_id=msg.user_id,image=True,url=url)
                except IndexError:
                    content = chat(user_id=msg.user_id,image=True,url=url)
                content, _ = safe_parse_chat_response(content)
                if if_tts:
                    rtf = tts(content)
                    await safe_set_input_status(event_type=0,user_id=msg.user_id)
                    await bot.api.post_private_msg(msg.user_id, rtf=rtf)
                else:
                    await safe_set_input_status(event_type=1,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, text=content)
                return

            reply_text = "这是被回复的消息："+ msg_obj.get("data").get("raw_message") +"。 "
            try:
                ori_content = msg.message[1].get("data").get("text")
            except IndexError:
                ori_content = "回复了你"
            content = chat(reply_text+ori_content, user_id=msg.user_id)
            content, _ = safe_parse_chat_response(content)
            if if_tts:
                rtf = tts(content)
                await safe_set_input_status(event_type=0,user_id=msg.user_id)
                await bot.api.post_private_msg(msg.user_id, rtf=rtf)
            else:
                await safe_set_input_status(event_type=1,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, text=content)
            return
    except IndexError:
        pass
    
    if msg.message[0].get("type") == "forward": # 处理转发消息
        msg_obj = await bot.api.get_msg(message_id=msg.message_id)
        print(msg_obj)
        res = deal_forward(msg_obj)
        content = chat(res, user_id=msg.user_id)
        content, _ = safe_parse_chat_response(content)
        if if_tts:
            rtf = tts(content)
            await safe_set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        else:
            await safe_set_input_status(event_type=1,user_id=msg.user_id)
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
        content, _ = safe_parse_chat_response(content)
        if if_tts:
            rtf = tts(content)
            await safe_set_input_status(event_type=0,user_id=msg.user_id)
            await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        else:
            await safe_set_input_status(event_type=1,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text=content)
        return

    if msg.raw_message and not msg.raw_message.startswith("/"):
        await _handle_plain_text_message(msg, msg.raw_message, if_tts)


async def handle_command(msg, is_group):
    clean = re.sub(r'\[CQ:[^]]*\]', '', msg.raw_message).strip()
    for command, handler in command_handlers.items():
        if isinstance(command, tuple):  # 处理命令别名情况
            for cmd in command:
                if re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', clean):           
                    _log.info(f"调用{cmd}命令")
                    # 记录指令到历史记录
                    try:
                        if is_group:
                            record_user_message(msg.raw_message, group_id=msg.group_id)
                        else:
                            record_user_message(msg.raw_message, user_id=msg.user_id)
                    except Exception:
                        pass
                    await handler(msg, is_group=is_group)
                    return 1
        elif re.match(fr'^{re.escape(command)}(?:\s|\.|$)', clean): # 处理单个命令情况
            _log.info(f"调用{command}命令")
            # 记录指令到历史记录
            try:
                if is_group:
                    record_user_message(msg.raw_message, group_id=msg.group_id)
                else:
                    record_user_message(msg.raw_message, user_id=msg.user_id)
            except Exception:
                pass
            await handler(msg, is_group=is_group)
            return 1
    return 0

if __name__ == "__main__":
    import threading
    import asyncio
    # 启动机器人
    bot_thread = threading.Thread(target=bot.run, kwargs={'enable_webui_interaction': False})
    bot_thread.start()

    # 启动每日自动总结任务
    def start_auto_summary():
        asyncio.run(auto_summary_task())
    threading.Thread(target=start_auto_summary, daemon=True).start()

    # 启动主动聊天任务
    def start_auto_active_chat():
        asyncio.run(auto_active_chat_task())
    threading.Thread(target=start_auto_active_chat, daemon=True).start()

    loop = asyncio.new_event_loop()
    _log.info("命令行模式已启动，可以在命令行内输入命令")
    try:
        pending = load_pending_jm_command()
        if pending:
            raw = pending.get("raw_message") or "/jm"
            uid = int(pending.get("user_id"))
            is_group = bool(pending.get("is_group"))
            if is_group:
                gid = int(pending.get("group_id"))
                message = {
                    "raw_message": raw,
                    "user_id": uid,
                    "group_id": gid
                }
                msg = GroupMessage(message)
            else:
                message = {
                    "raw_message": raw,
                    "user_id": uid
                }
                msg = PrivateMessage(message)
            loop.run_until_complete(handle_jmcomic(msg, is_group=is_group, from_pending_restart=True))
            clear_pending_jm_command()
    except Exception as e:
        _log.error(f"处理待执行jm命令失败: {e}")
    while True:
        try:
            command = input("").strip()
            if not command:
                continue
            if command.lower() == 'exit':
                _log.info("已退出命令行模式")
                break
            message = {
                "raw_message":command,
                "user_id":admin_id
            }
            msg = GroupMessage(message)
            loop.run_until_complete(handle_command(msg, is_group=False))
            time.sleep(1)
        except EOFError:
            _log.info("标准输入已关闭，命令行模式结束")
            break
        except KeyboardInterrupt:
            _log.info("退出命令输入")
            break
