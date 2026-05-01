from ncatbot.core import BotClient, GroupMessage, PrivateMessage, BotAPI
from ncatbot.utils.logger import get_log
from nbot.core.heartbeat import HeartbeatCore

from nbot.web.utils.config_loader import load_config
from nbot.services.chat_service import group_messages, user_messages, chat, generate_today_summary, summarize_group_text
from nbot.services.chat_service import delete_session_workspace, get_qq_session_id, WORKSPACE_AVAILABLE
from nbot.services.ai import ai_client
from nbot.services.tts import tts
import base64
import hashlib
import html
import jmcomic
import requests
import random
import configparser
import json
import yaml
import re
import os
import asyncio
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from jmcomic import *
from typing import Dict, List
from datetime import datetime
from PIL import Image as PILImage
from ncatbot.core import (
    MessageChain,  
    Music,          
)
#----------------------
# region 全局变量设置
#----------------------

if_tts = False #判断是否开启TTS

_log = get_log()

def normalize_file_path(path: str) -> str:
    """
    标准化文件路径，将Windows反斜杠转换为正斜杠，
    并使用绝对路径，确保ncatbot能正确识别
    """
    return os.path.abspath(path).replace("\\", "/")

bot_id,admin_id = load_config() # 加载配置,返回机器人qq号

bot = BotClient()
heartbeat_core = HeartbeatCore(bot.api)

# ----------------------
# region 统一消息发送与记录
# ----------------------
# 记录机器人发送的所有消息到历史记录中
# 通过直接补丁 BotAPI 和消息类，确保所有发送方式都能被记录

# 防止重复应用补丁
if not hasattr(BotAPI, '_nbot_patched'):
    original_post_private_msg = BotAPI.post_private_msg
    original_post_group_msg = BotAPI.post_group_msg
    original_group_reply = GroupMessage.reply
    original_private_reply = PrivateMessage.reply
    pending_group_reply_context = {}

    async def wrapped_post_private_msg(self, user_id, **kwargs):
        content = kwargs.get('text', '')
        if content and isinstance(content, str):
            try:
                from chat import record_assistant_message
                record_assistant_message(content, user_id=user_id)
            except Exception:
                pass
        return await original_post_private_msg(self, user_id, **kwargs)

    async def wrapped_post_group_msg(self, group_id, **kwargs):
        content = kwargs.get('text', '')
        if content and isinstance(content, str):
            try:
                from chat import record_assistant_message, log_to_group_full_file
                context_key = (str(group_id), content)
                pending_users = pending_group_reply_context.get(context_key, [])
                group_user_id = pending_users.pop(0) if pending_users else kwargs.get("group_user_id")
                if pending_users:
                    pending_group_reply_context[context_key] = pending_users
                else:
                    pending_group_reply_context.pop(context_key, None)
                record_assistant_message(
                    content,
                    group_id=group_id,
                    group_user_id=group_user_id,
                )
                log_to_group_full_file(group_id, bot_id, "机器人", content)
            except Exception:
                pass
        return await original_post_group_msg(self, group_id, **kwargs)

    async def wrapped_group_reply(self, text=None, **kwargs):
        content = text if isinstance(text, str) else kwargs.get("text", "")
        if content and isinstance(content, str):
            context_key = (str(self.group_id), content)
            pending_group_reply_context.setdefault(context_key, []).append(str(self.user_id))
        return await original_group_reply(self, text=text, **kwargs)

    # 应用补丁到类级别
    BotAPI.post_private_msg = wrapped_post_private_msg
    BotAPI.post_group_msg = wrapped_post_group_msg
    GroupMessage.reply = wrapped_group_reply
    BotAPI._nbot_patched = True
# ----------------------

command_handlers = {}

user_favorites: Dict[str, List[str]] = {}  # 用户收藏夹 {user_id: [comic_ids]}
group_favorites: Dict[str, Dict[str, List[str]]] = {}  # 群组收藏夹 {group_id: {user_id: [comic_ids]}}

admin = [str(admin_id)]  # 确保admin_id是字符串形式

black_list_comic = {"global": [], "groups": {}, "users": {}} # str,黑名单

running = {}  #用于定时聊天的开关
tasks = {}  # 用于存储聊天的定时任务

books = {}

smtp_config = {}
user_email = {}

schedule_tasks = {} #用于存储定时任务

at_all_group = [] # 用于存储@全体成员的群

# ------------------
# region 通用函数
# ------------------

def read_at_all_group():
    try:
        with open(os.path.join(load_address(),"at_all_group.txt"), "r", encoding="utf-8") as f:
            group_ids = f.readlines()
            for i in range(len(group_ids)):
                group_ids[i] = group_ids[i].strip()
            at_all_group.extend(group_ids)
    except FileNotFoundError:
        write_at_all_group()

def write_at_all_group():
    with open(os.path.join(load_address(),"at_all_group.txt"), "w", encoding="utf-8") as f:
        for group_id in at_all_group:
            f.write(group_id + "\n")

def write_admin():
    try:
        with open("admin.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(admin) + "\n")  # 每行一个管理员ID
    except Exception as e:
        _log.error(f"写入管理员文件失败: {e}")

def load_admin():
    try:
        with open("admin.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line != str(admin_id):
                    admin.append(line)
    except FileNotFoundError:
        write_admin()

def register_command(*command,help_text = None,admin_show = False,category = "1"): # 注册命令
    """
    装饰器，用于注册命令。
    :param command: 命令名称，支持多个。
    :param help_text: 命令的帮助文本。
    :param admin_show: 是否在帮助中显示管理员命令，默认False。
    :param category: 命令所属分类，默认"1"。
    """
    def decorator(func):
        command_handlers[command] = func
        func.help_text = help_text
        func.admin_show = admin_show
        func.category = category
        return func
    return decorator

def load_address(): # 加载配置文件，返回图片保存地址（绝对路径）
    """
    加载配置文件，返回缓存目录的绝对路径
    支持跨平台（Windows/Linux/Mac）
    """
    # 获取项目根目录（即包含 nbot 目录的目录）
    # 使用当前文件所在位置来确定项目根目录
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    # 从 nbot/commands.py 回到项目根目录
    project_root = os.path.dirname(current_file_dir)

    config_path = os.path.join(project_root, "resources", "config", "option.yml")

    with open(config_path, "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
        after_photo_list = conf.get("plugins", {}).get("after_album", [])
        if after_photo_list and isinstance(after_photo_list, list):
            pdf_dir = after_photo_list[0].get("kwargs", {}).get("pdf_dir", "./cache/pdf/")
        else:
            pdf_dir = "./cache/pdf/"

    # 将相对路径转换为绝对路径
    if not os.path.isabs(pdf_dir):
        # 如果是相对路径，基于项目根目录解析
        pdf_dir = os.path.join(project_root, pdf_dir)

    pdf_dir = os.path.normpath(pdf_dir)
    cache_dir = os.path.dirname(pdf_dir)  # 返回pdf目录的父目录

    # 确保目录存在
    os.makedirs(cache_dir, exist_ok=True)

    return cache_dir

def load_favorites():
    """加载收藏夹数据"""
    cache_dir = os.path.join(load_address(),"list")
    os.makedirs(cache_dir, exist_ok=True)
    
    # 加载用户收藏
    user_file = os.path.join(cache_dir, "user_favorites.json")
    if os.path.exists(user_file):
        with open(user_file, 'r', encoding='utf-8') as f:
            user_favorites.update(json.load(f))
    
    # 加载群组收藏
    group_file = os.path.join(cache_dir, "group_favorites.json")
    if os.path.exists(group_file):
        with open(group_file, 'r', encoding='utf-8') as f:
            group_favorites.update(json.load(f))

def load_smtp_config():
    global smtp_config
    try:
        with open("smtp_config.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "host" in data:
                smtp_config = {"global": data}
            else:
                smtp_config = data
    except FileNotFoundError:
        smtp_config = {}

def save_smtp_config():
    with open("smtp_config.json", "w", encoding="utf-8") as f:
        json.dump(smtp_config, f, ensure_ascii=False, indent=2)

def load_email_config():
    global user_email
    try:
        with open("email_config.json", "r", encoding="utf-8") as f:
            user_email = json.load(f)
    except FileNotFoundError:
        user_email = {}

def save_email_config():
    with open("email_config.json", "w", encoding="utf-8") as f:
        json.dump(user_email, f, ensure_ascii=False, indent=2)

def save_favorites():
    """保存收藏夹数据"""
    cache_dir = os.path.join(load_address(),"list/")
    os.makedirs(cache_dir, exist_ok=True)
    
    # 保存用户收藏
    with open(os.path.join(cache_dir, "user_favorites.json"), 'w', encoding='utf-8') as f:
        json.dump(user_favorites, f, ensure_ascii=False, indent=2)
    
    # 保存群组收藏
    with open(os.path.join(cache_dir, "group_favorites.json"), 'w', encoding='utf-8') as f:
        json.dump(group_favorites, f, ensure_ascii=False, indent=2)

async def schedule_task(delay_hours: float, task_func, *args, **kwargs):
    """延时执行任务
    :param delay_hours: 延迟的小时数
    :param task_func: 要执行的函数
    """
    await asyncio.sleep(delay_hours * 3600)  # 转换为秒
    await task_func(*args, **kwargs)

async def schedule_task_by_date(target_time: datetime, task_func, *args, **kwargs):
    """精确时间执行任务
    :param target_time: 目标日期时间(datetime对象)
    :param task_func: 要执行的函数
    """
    now = datetime.now()
    if target_time < now:
        raise ValueError("目标时间不能是过去时间喵~")
    delay_seconds = (target_time - now).total_seconds()
    await asyncio.sleep(delay_seconds)
    await task_func(*args, **kwargs)

async def schedule_job_task(delay_hours: float,loop:bool,name:str, task_func, *args, **kwargs):
    """延时执行任务
    :param delay_hours: 延迟的小时数
    :param loop: 是否循环执行
    :param name: 任务名称
    :param task_func: 要执行的函数
    """
    if loop:
        while True:
            await asyncio.sleep(delay_hours * 3600)  # 转换为秒
            await task_func(*args, **kwargs)
            _log.info(f"任务 {name} 执行完成")
    else:
        await asyncio.sleep(delay_hours * 3600)  # 转换为秒
        await task_func(*args, **kwargs)
        _log.info(f"任务 {name} 执行完成")
        del schedule_tasks[name]

async def chatter(id):
    """
    定时聊天函数。
    :param msg: 消息对象。
    """
    content = chat(content="现在请你根据上下文，主动和用户聊天",user_id=id)
    content, _ = safe_parse_chat_response(content)
    if if_tts:
        rtf = tts(content)
        await bot.api.post_private_msg(id, rtf=rtf)
        await bot.api.post_private_msg(id, text=content)    
    else:
        await bot.api.post_private_msg(id, text=content)

async def chat_loop(id:str):
    """
    单人定时聊天任务
    :param id: QQ号(msg.user_id)
    """
    global running
    running[id]["state"] = True
    write_running()
    
    while True:
        # 检查是否仍处于激活状态
        if not running.get(id, {}).get("active", False):
            running[id]["state"] = False
            write_running()
            break
            
        try:
            date_time = datetime.now()
            current_time = time.time()
            last_time = running[id].get("last_time", 0)
            
            # 只在8点到24点之间运行
            if date_time.hour < 8 or date_time.hour >= 24:
                await asyncio.sleep(60 * 10)  # 10分钟检查一次
                continue
                
            # 计算剩余等待时间
            time_remaining = (60 * 60 * running[id]["interval"]) - (current_time - last_time)
            
            # 如果还没到时间，精确等待剩余时间
            if time_remaining > 0:
                await asyncio.sleep(min(time_remaining, 60 * 10))  # 最多等待10分钟
                continue
                
            # 发送聊天消息
            await chatter(id)
            running[id]["last_time"] = current_time
            write_running()
            
            # 等待完整间隔时间
            await asyncio.sleep(60 * 60 * running[id]["interval"])
            
        except Exception as e:
            _log.error(f"主动聊天循环出错: {e}")
            await asyncio.sleep(60)  # 出错后等待1分钟再重试

def write_blak_list():
    """
    写入黑名单
    """
    cache_dir = os.path.join(load_address(),"black_list/")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"blak_list.json"), "w", encoding="utf-8") as f:
            json.dump(black_list_comic, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log.error(f"写入黑名单文件失败: {e}")

def load_blak_list():
    """
    加载黑名单
    """
    cache_dir = os.path.join(load_address(),"black_list/")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"blak_list.json"), "r", encoding="utf-8") as f:
            black_list_comic.update(json.load(f))

    except FileNotFoundError:
        write_blak_list()

def write_running():
    """
    写入定时聊天开关
    """
    cache_dir = os.path.join(load_address(),"running/")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"running.json"), "w", encoding="utf-8") as f:
            json.dump(running, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log.error(f"写入定时聊天开关文件失败: {e}")

def normalize_timestamp(ts):
    try:
        value = float(ts)
    except Exception:
        return 0.0
    if value > 1e11:
        return value / 1000.0
    return value

def load_running():
    """
    加载定时聊天开关
    """
    cache_dir = os.path.join(load_address(),"running/")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"running.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            for uid, info in data.items():
                if isinstance(info, dict) and "last_time" in info:
                    info["last_time"] = normalize_timestamp(info["last_time"])
            running.update(data)
    except FileNotFoundError:
        write_running()

def update_user_active_chat_time(user_id):
    """
    当用户主动发消息时，更新最后活跃时间。
    这会推迟机器人的下一次主动聊天。
    """
    user_id = str(user_id)
    if user_id in running and running[user_id].get("active", False):
        running[user_id]["last_time"] = time.time()
        write_running()

def update_running(id):
    if id in tasks:
        tasks[id].cancel()  # 取消之前的任务
        tasks[id] = asyncio.create_task(chat_loop(id))  # 创建新的任务
        
def load_novel_data():
    """
    加载小说数据
    """
    with open("resources/config/novel_details2.json", "r", encoding="utf-8") as f:
        books.update(json.load(f))

def fetch_cover_url(id:str, client=None) -> str:
    """
    获取指定本子的第一张图片URL
    :param album_id: 本子ID
    :return: 第一张图片的URL
    """
    filename = "00001.webp"
    fallback_url = f"https://cdn-msp3.jmapinodeudzn.net/media/photos/{id}/{filename}"
    cache_dir = os.path.join(load_address(), "jm_cover_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{id}.jpg")

    try:
        if not os.path.exists(cache_path):
            client = client or JmOption.default().new_jm_client()
            album = client.get_album_detail(id)
            decoded_path = os.path.join(cache_dir, f"{id}_decoded.jpg")
            client.download_image(
                fallback_url,
                decoded_path,
                int(album.scramble_id),
                decode_image=True,
            )

            with PILImage.open(decoded_path) as image:
                image = image.convert("RGB")
                image.thumbnail((260, 380))
                image.save(cache_path, format="JPEG", quality=72, optimize=True)

            if os.path.exists(decoded_path):
                os.remove(decoded_path)

        with open(cache_path, "rb") as file_obj:
            encoded = base64.b64encode(file_obj.read()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    except Exception as e:
        _log.warning(f"获取JM封面失败，回退原图链接: album_id={id}, error={e}")
        return fallback_url


# ----------------------
# 公共 HTML 卡片模板
# ----------------------

def build_jm_grid_html(title: str, filepath: str):
    """写入 JM 卡片网格的 HTML 头部（需要调用方后续追加 card，再调用 close_jm_grid_html）"""
    html_head = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: 'Segoe UI', 'PingFang SC', sans-serif; background:#111827; color:#f3f4f6; margin:0; padding:24px; }}
    h1 {{ font-size:24px; margin:0 0 20px; }}
    .note {{ color:#9ca3af; font-size:13px; margin:-8px 0 18px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:18px; }}
    .card {{ background:#1f2937; border:1px solid rgba(255,255,255,0.08); border-radius:16px; overflow:hidden; box-shadow:0 10px 24px rgba(0,0,0,0.22); transition: transform 0.2s, box-shadow 0.2s; }}
    .card:hover {{ transform: translateY(-4px); box-shadow:0 14px 28px rgba(0,0,0,0.3); }}
    .card a {{ text-decoration:none; color:inherit; display:block; }}
    .cover {{ width:100%; aspect-ratio: 13 / 18; object-fit:cover; display:block; background:#0b1220; }}
    .meta {{ padding:12px 14px 16px; }}
    .num {{ color:#60a5fa; font-weight:700; font-size:13px; margin-bottom:6px; }}
    .title {{ font-size:14px; line-height:1.45; word-break:break-word; }}
    .aid {{ color:#9ca3af; font-size:12px; margin-top:8px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="note">前 {JM_RANK_DECODE_LIMIT} 张封面会做顺序修复，后续封面直接展示以保证生成速度。</div>
  <div class="grid">
"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_head)


def append_jm_card(filepath: str, album_id: int, title: str, seq: int, client=None):
    """追加一张卡片到已打开的 HTML 文件（流式写入）"""
    if seq <= JM_RANK_DECODE_LIMIT:
        cover_url = fetch_cover_url(str(album_id), client=client)
    else:
        cover_url = f"https://cdn-msp3.jmapinodeudzn.net/media/photos/{album_id}/00001.webp"
    album_url = f"https://jmcm.la/album/{album_id}"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(
            f'    <article class="card">'
            f'<a href="{html.escape(album_url, quote=True)}" target="_blank">'
            f'<img class="cover" src="{html.escape(cover_url, quote=True)}" alt="{html.escape(title, quote=True)}">'
            f'<div class="meta">'
            f'<div class="num">#{seq}</div>'
            f'<div class="title">{html.escape(title)}</div>'
            f'<div class="aid">ID: {html.escape(str(album_id))}</div>'
            f'</div></a></article>\n'
        )


def close_jm_grid_html(filepath: str):
    """关闭 HTML 网格文件"""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write("  </div>\n</body>\n</html>\n")


def build_novel_grid_html(title: str, filepath: str):
    """写入轻小说卡片网格的 HTML 头部"""
    html_head = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: 'Segoe UI', 'PingFang SC', sans-serif; background:#111827; color:#f3f4f6; margin:0; padding:24px; }}
    h1 {{ font-size:24px; margin:0 0 20px; }}
    .note {{ color:#9ca3af; font-size:13px; margin:-8px 0 18px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:18px; }}
    .card {{ background:#1f2937; border:1px solid rgba(255,255,255,0.08); border-radius:16px; overflow:hidden; box-shadow:0 10px 24px rgba(0,0,0,0.22); transition: transform 0.2s, box-shadow 0.2s; }}
    .card:hover {{ transform: translateY(-4px); box-shadow:0 14px 28px rgba(0,0,0,0.3); }}
    .card a {{ text-decoration:none; color:inherit; display:block; }}
    .cover {{ width:100%; aspect-ratio: 3 / 4; object-fit:cover; display:block; background:#0b1220; }}
    .meta {{ padding:12px 14px 16px; }}
    .author {{ color:#60a5fa; font-weight:700; font-size:13px; margin-bottom:6px; }}
    .title {{ font-size:14px; line-height:1.45; word-break:break-word; }}
    .info {{ color:#9ca3af; font-size:12px; margin-top:8px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="note">点击卡片查看小说详情或下载喵~</div>
  <div class="grid">
"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_head)


def append_novel_card(filepath: str, book_id: str, title: str, author: str, seq: int, cover_url: str = None):
    """追加一张轻小说卡片到已打开的 HTML 文件"""
    node = int(book_id) // 1000 if book_id.isdigit() else 0
    if not cover_url:
        cover_url = f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg"
    page_url = f"https://www.wenku8.net/book/{book_id}.htm"
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(
            f'    <article class="card">'
            f'<a href="{html.escape(page_url, quote=True)}" target="_blank">'
            f'<img class="cover" src="{html.escape(cover_url, quote=True)}" alt="{html.escape(title, quote=True)}">'
            f'<div class="meta">'
            f'<div class="author">{html.escape(author)}</div>'
            f'<div class="title">{html.escape(title)}</div>'
            f'<div class="info">ID: {html.escape(book_id)}</div>'
            f'</div></a></article>\n'
        )


def close_novel_grid_html(filepath: str):
    """关闭轻小说 HTML 网格文件"""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write("  </div>\n</body>\n</html>\n")


def build_novel_detail_html(title: str, info: dict, filepath: str):
    """生成单本轻小说详情页 HTML"""
    book_id_match = re.search(r'/book/(\d+)\.htm', info.get('page', ''))
    book_id = book_id_match.group(1) if book_id_match else "0"
    node = int(book_id) // 1000 if book_id.isdigit() else 0
    cover_url = info.get('cover_url') or f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg"
    page_url = info.get('page') or f"https://www.wenku8.net/book/{book_id}.htm"
    download_url = info.get('download_url') or f"https://dl.wenku8.com/down.php?type=txt&node={node}&id={book_id}"
    
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)} - 轻小说详情</title>
  <style>
    body {{ font-family: 'Segoe UI', 'PingFang SC', sans-serif; background:#111827; color:#f3f4f6; margin:0; padding:24px; }}
    .card {{ background:#1f2937; border:1px solid rgba(255,255,255,0.08); border-radius:16px; overflow:hidden; box-shadow:0 10px 24px rgba(0,0,0,0.22); max-width:500px; margin:0 auto; }}
    .cover {{ width:100%; aspect-ratio: 3 / 4; object-fit:cover; display:block; background:#0b1220; }}
    .meta {{ padding:20px; }}
    .title {{ font-size:20px; font-weight:700; margin-bottom:12px; line-height:1.4; }}
    .author {{ color:#60a5fa; font-size:14px; margin-bottom:16px; }}
    .info-row {{ display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.05); font-size:13px; }}
    .info-label {{ color:#9ca3af; }}
    .info-value {{ color:#f3f4f6; }}
    .intro {{ margin-top:16px; padding:12px; background:rgba(0,0,0,0.2); border-radius:8px; font-size:13px; line-height:1.6; }}
    .intro-title {{ color:#60a5fa; font-weight:600; margin-bottom:8px; }}
    .actions {{ display:flex; gap:12px; margin-top:20px; }}
    .btn {{ flex:1; padding:12px; border-radius:8px; text-align:center; text-decoration:none; font-size:14px; font-weight:600; transition:opacity 0.2s; }}
    .btn:hover {{ opacity:0.9; }}
    .btn-primary {{ background:#4a9eff; color:white; }}
    .btn-secondary {{ background:#374151; color:white; }}
  </style>
</head>
<body>
  <div class="card">
    <img class="cover" src="{html.escape(cover_url, quote=True)}" alt="{html.escape(title, quote=True)}">
    <div class="meta">
      <div class="title">{html.escape(title)}</div>
      <div class="author">作者：{html.escape(info.get('author', '未知'))}</div>
      <div class="info-row">
        <span class="info-label">分类</span>
        <span class="info-value">{html.escape(info.get('category', '未知'))}</span>
      </div>
      <div class="info-row">
        <span class="info-label">字数</span>
        <span class="info-value">{html.escape(info.get('word_count', '未知'))}</span>
      </div>
      <div class="info-row">
        <span class="info-label">状态</span>
        <span class="info-value">{html.escape(info.get('is_serialize', '未知'))}</span>
      </div>
      <div class="info-row">
        <span class="info-label">更新日期</span>
        <span class="info-value">{html.escape(info.get('last_date', '未知'))}</span>
      </div>
      <div class="info-row">
        <span class="info-label">热度</span>
        <span class="info-value">{html.escape(str(info.get('hot', '未知')))}</span>
      </div>
      <div class="intro">
        <div class="intro-title">简介</div>
        <div>{html.escape(info.get('introduction', '暂无简介'))}</div>
      </div>
      <div class="actions">
        <a href="{html.escape(page_url, quote=True)}" target="_blank" class="btn btn-secondary">查看详情</a>
        <a href="{html.escape(download_url, quote=True)}" target="_blank" class="btn btn-primary">下载小说</a>
      </div>
    </div>
  </div>
</body>
</html>"""
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)


class SwitchManager:
    """
    开关管理器，支持群聊和个人开关的批量管理
    群聊开关以群为整体
    """
    
    def __init__(self):
        # 存储所有开关状态
        self.group_switches = {}  # {group_id: {switch_name: bool}}
        self.user_switches = {}   # {user_id: {switch_name: bool}}
        self.switch_configs = {}   # {switch_name: {'default': bool, 'description': str}}
        
        # 初始化默认开关
        self._init_default_switches()
    
    def _init_default_switches(self):
        """初始化默认开关配置"""
        self.switch_configs = {

        }
    
    def add_switch(self, switch_name: str, default_value: bool = False, description: str = ""):
        """添加新的开关类型"""
        self.switch_configs[switch_name] = {
            'default': default_value,
            'description': description
        }
        self.save_switches()
    
    def get_switch_state(self, switch_name: str, group_id: str = None, user_id: str = None):
        """获取开关状态"""
        if switch_name not in self.switch_configs:
            raise ValueError(f"未知的开关类型: {switch_name}")
        
        # 优先检查用户开关
        if user_id and user_id in self.user_switches:
            if switch_name in self.user_switches[user_id]:
                return self.user_switches[user_id][switch_name]
        
        # 检查群聊开关
        if group_id and group_id in self.group_switches:
            if switch_name in self.group_switches[group_id]:
                return self.group_switches[group_id][switch_name]
        
        # 返回默认值
        return self.switch_configs[switch_name]['default']
    
    def set_switch_state(self, switch_name: str, state: bool, group_id: str = None, user_id: str = None):
        """设置开关状态"""
        if switch_name not in self.switch_configs:
            raise ValueError(f"未知的开关类型: {switch_name}")
        
        if user_id:
            if user_id not in self.user_switches:
                self.user_switches[user_id] = {}
            self.user_switches[user_id][switch_name] = state
        elif group_id:
            if group_id not in self.group_switches:
                self.group_switches[group_id] = {}
            self.group_switches[group_id][switch_name] = state
        else:
            raise ValueError("必须提供group_id或user_id")
        self.save_switches()
    
    def toggle_switch(self, switch_name: str, group_id: str = None, user_id: str = None):
        """切换开关状态"""
        current_state = self.get_switch_state(switch_name, group_id, user_id)
        new_state = not current_state
        self.set_switch_state(switch_name, new_state, group_id, user_id)
        self.save_switches()
        return new_state
    
    def get_switch_info(self, switch_name: str):
        """获取开关信息"""
        if switch_name not in self.switch_configs:
            return None
        return self.switch_configs[switch_name]
    
    def list_all_switches(self):
        """列出所有开关类型"""
        return list(self.switch_configs.keys())
    
    def save_switches(self, file_path: str = "switches.json"):
        """
        保存开关状态到文件
        :param file_path: 保存路径
        """
        data = {
            'group_switches': self.group_switches,
            'user_switches': self.user_switches
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def load_switches(self, file_path: str = "switches.json"):
        """
        从文件加载开关状态
        :param file_path: 加载路径
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.group_switches = data.get('group_switches', {})
                self.user_switches = data.get('user_switches', {})
        except FileNotFoundError:
            # 文件不存在时使用默认值
            pass

#-------------------------
#     region 加载参数
#-------------------------

load_favorites()
load_admin()
load_blak_list()
load_running()
load_novel_data()
read_at_all_group()
load_smtp_config()
load_email_config()
switch = SwitchManager() #加载开关
switch.load_switches()
switch.add_switch('tts', default_value=False, description='TTS语音开关')
switch.add_switch('jm_send', default_value=True, description='漫画发送开关')
switch.add_switch('jm_send_user', default_value=False, description='用户私信发送漫画开关')
switch.add_switch('command', default_value=True, description='命令开关')
switch.add_switch('pdf_password', default_value=False, description='PDF密码开关')
switch.add_switch('summary_auto', default_value=False, description='每日自动总结开关')
switch.add_switch('active_chat', default_value=False, description='主动聊天开关')
switch.add_switch('auto_reply', default_value=False, description='群聊智能自动回复开关')
switch.add_switch('jm_send_email', default_value=False, description='漫画邮箱发送开关')
switch.save_switches()

#----------------------
#     region 命令
#----------------------

@register_command("/tts",help_text = "/tts -> 开启或关闭TTS(admin)",admin_show = True,category = "4")
async def handle_tts(msg, is_group=True):
    if str(msg.user_id) not in admin:
        if is_group:
            await msg.reply(text="你没有权限使用此命令喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="你没有权限使用此命令喵~")
        return
    if_tts = switch.toggle_switch('tts', group_id=str(msg.group_id) if is_group else None, user_id=str(msg.user_id) if not is_group else None)

    text = "已开启TTS喵~" if if_tts else "已关闭TTS喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)
    switch.save_switches()

# ---------------漫画类命令----------------
comic_cache = []
JM_RANK_DECODE_LIMIT = 50
@register_command("/jmrank",help_text = "/jmrank <月排行/周排行> -> 获取排行榜",category = "1")
async def handle_jmrank(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在获取排行喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在获取排行喵~")
    select = msg.raw_message[len("/jmrank"):].strip()
    op = JmOption.default()
    cl = op.new_jm_client()
    page: JmCategoryPage = cl.categories_filter(
        page=1,
        time=JmMagicConstants.TIME_ALL,
        category=JmMagicConstants.CATEGORY_ALL,
        order_by=JmMagicConstants.ORDER_BY_LATEST,
    )
    if select == "月排行":
        page: JmCategoryPage = cl.month_ranking(1)
    elif select == "周排行":
        page: JmCategoryPage = cl.week_ranking(1)
    else:
        page: JmCategoryPage = cl.week_ranking(1)
    cache_dir = os.path.join(load_address(),"rank")
    os.makedirs(cache_dir,exist_ok = True)
    # 使用唯一文件名，避免工作区/预览链按同名文件命中旧缓存
    file_token = hashlib.md5(f"{select}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{select}.html"
    filepath = os.path.join(cache_dir, filename)
    comic_cache.clear()

    build_jm_grid_html(f"{html.escape(select)} · JM 排行", filepath)

    tot = 0
    for aid, atitle in page:
        tot += 1
        append_jm_card(filepath, aid, atitle, tot, client=cl)
        comic_cache.append(aid)
        if tot >= 50:
            break

    close_jm_grid_html(filepath)

    if not os.path.exists(filepath):
        if is_group:
            await msg.reply(text="获取排行失败喵~，文件不存在")
        else:
            await bot.api.post_private_msg(msg.user_id, text="获取排行失败喵~，文件不存在")
        return
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/jm_search",help_text = "/jm_search <内容> -> 搜索漫画",category = "1")
async def handle_search(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = os.path.join(load_address(),"search")
    os.makedirs(cache_dir,exist_ok = True)
    client = JmOption.default().new_jm_client()
    content = msg.raw_message[len("/jm_search"):].strip()

    if not content or content == " ":
        if is_group:
            await msg.reply(text="搜索内容不能为空喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="搜索内容不能为空喵~")
        return

    file_token = hashlib.md5(f"{content}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{content}.html"
    filepath = os.path.join(cache_dir, filename)
    comic_cache.clear()

    if re.match(r'^\d+$', content):  # 检查是否为纯数字，搜索单个本子
        id = content
        # 直接搜索禁漫车号，生成单本卡片HTML
        album: JmAlbumDetail = client.get_album_detail(id)
        cover_url = fetch_cover_url(id, client=client)
        album_url = f"https://jmcm.la/album/{id}"
        html_head = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(album.title)} - JM 本子</title>
  <style>
    body {{ font-family: 'Segoe UI', 'PingFang SC', sans-serif; background:#111827; color:#f3f4f6; margin:0; padding:24px; }}
    .card {{ background:#1f2937; border:1px solid rgba(255,255,255,0.08); border-radius:16px; overflow:hidden; box-shadow:0 10px 24px rgba(0,0,0,0.22); max-width:400px; margin:0 auto; transition: transform 0.2s, box-shadow 0.2s; cursor:pointer; }}
    .card:hover {{ transform: translateY(-4px); box-shadow:0 14px 28px rgba(0,0,0,0.3); }}
    .cover {{ width:100%; aspect-ratio: 13 / 18; object-fit:cover; display:block; background:#0b1220; }}
    .meta {{ padding:16px 20px 20px; }}
    .title {{ font-size:18px; font-weight:700; line-height:1.4; word-break:break-word; margin-bottom:10px; }}
    .info {{ color:#9ca3af; font-size:13px; line-height:1.8; }}
    .tag {{ display:inline-block; background:#374151; border-radius:6px; padding:2px 8px; margin:2px; font-size:12px; }}
  </style>
</head>
<body>
  <a href="{html.escape(album_url, quote=True)}" target="_blank" style="text-decoration:none; color:inherit;">
  <div class="card">
    <img class="cover" src="{html.escape(cover_url, quote=True)}" alt="{html.escape(album.title, quote=True)}">
    <div class="meta">
      <div class="title">{html.escape(album.title)}</div>
      <div class="info">
        <div>ID: {html.escape(str(id))}</div>
        <div>页数: {album.page_count}</div>
        <div>浏览: {album.views}</div>
        <div>评论: {album.comment_count}</div>
        <div style="margin-top:8px;">{''.join(f'<span class="tag">{html.escape(str(tag))}</span>' for tag in album.tags)}</div>
      </div>
    </div>
  </div>
  </a>
</body>
</html>"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_head)
        if is_group:
            await bot.api.post_group_file(msg.group_id, file=filepath)
        else:
            await bot.api.upload_private_file(msg.user_id, filepath, filename)
        return

    # 多本搜索，生成卡片网格HTML
    build_jm_grid_html(f"{html.escape(content)} · JM 搜索", filepath)

    tot = 0
    for i in range(5):  # 搜索5页
        page: JmSearchPage = client.search_site(search_query=content, page=i+1)
        if len(page) == 0:
            break
        for album_id, title in page:
            tot += 1
            append_jm_card(filepath, album_id, title, tot, client=client)
            comic_cache.append(album_id)
        if tot >= 50:
            break

    close_jm_grid_html(filepath)

    if not os.path.exists(filepath):
        if is_group:
            await msg.reply(text="搜索失败喵~，文件不存在")
        else:
            await bot.api.post_private_msg(msg.user_id, text="搜索失败喵~，文件不存在")
        return

    if is_group:
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/jm_tag",help_text = "/jm_tag <标签> -> 搜索漫画标签",category = "1")
async def handle_tag(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = os.path.join(load_address(),"search")
    os.makedirs(cache_dir,exist_ok = True)
    content = msg.raw_message[len("/tag"):].strip()
    client = JmOption.default().new_jm_client()

    file_token = hashlib.md5(f"tag_{content}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{content}.html"
    filepath = os.path.join(cache_dir, filename)
    comic_cache.clear()

    build_jm_grid_html(f"{html.escape(content)} · JM 标签搜索", filepath)

    tot = 0
    for i in range(5):  # 搜索5页
        page: JmSearchPage = client.search_tag(search_query=content, page=i+1)
        if len(page) == 0:
            break
        for album_id, title in page:
            tot += 1
            append_jm_card(filepath, album_id, title, tot, client=client)
            comic_cache.append(album_id)
        if tot >= 50:
            break

    close_jm_grid_html(filepath)

    if is_group:
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/get_fav",help_text = "/get_fav <用户名> <密码> -> 获取收藏夹(群聊请私聊)",category = "1")
async def handle_get_fav(msg, is_group=True):
    match = re.match(r'^/get_fav\s+(\S+)\s+(\S+)$', msg.raw_message)
    if not match:
        error_msg = "格式错误喵~ 请输入 /get_fav 用户名 密码"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)
        return

    username = match.group(1)
    password = match.group(2)

    if is_group:
        await msg.reply(text="正在获取收藏夹喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在获取收藏夹喵~")

    cache_dir = os.path.join(load_address(),"fav")
    os.makedirs(cache_dir, exist_ok=True)

    file_token = hashlib.md5(f"fav_{username}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{username}.html"
    filepath = os.path.join(cache_dir, filename)
    comic_cache.clear()

    option = JmOption.default()
    cl = option.new_jm_client()
    try:
        cl.login(username, password)  # 也可以使用login插件/配置cookies
    except Exception as e:
        if is_group:
            await msg.reply(text=f"登录失败喵~：{e}")
        else:
            await bot.api.post_private_msg(msg.user_id, text=f"登录失败喵~：{e}")
        return

    build_jm_grid_html(f"{html.escape(username)} · JM 收藏夹", filepath)

    tot = 0
    # 遍历全部收藏的所有页
    for page in cl.favorite_folder_gen():  # 如果你只想获取特定收藏夹，需要添加folder_id参数
        for aid, atitle in page.iter_id_title():
            tot += 1
            append_jm_card(filepath, aid, atitle, tot, client=cl)
            comic_cache.append(aid)
        if tot >= 50:
            break

    close_jm_grid_html(filepath)

    if is_group:
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/jm",help_text = "/jm <漫画ID> -> 下载漫画",category = "1")
async def handle_jmcomic(msg, is_group=True):
    match = re.match(r'^/jm\s+(\d+)$', msg.raw_message)
    if match:
        comic_id = match.group(1)
        # 检查是否在全局、群组或用户黑名单中
        if comic_id in black_list_comic["global"]:
            error_msg = "该漫画已被加入黑名单喵~"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
            return
        if is_group:
            group_id = str(msg.group_id)
            if group_id in black_list_comic["groups"] and comic_id in black_list_comic["groups"][group_id]:
                error_msg = "该漫画已被加入本群黑名单喵~"
                await msg.reply(text=error_msg)
                return
        else:
            user_id = str(msg.user_id)
            if user_id in black_list_comic["users"] and comic_id in black_list_comic["users"][user_id]:
                error_msg = "该漫画已被加入你的黑名单喵~"
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
                return

        pdf_path = normalize_file_path(os.path.join(load_address(),f"pdf/{comic_id}.pdf"))
        if os.path.exists(pdf_path):
            file_size = os.path.getsize(pdf_path)
            if is_group:
                if switch.get_switch_state('jm_send', group_id=str(msg.group_id)):
                    if switch.get_switch_state('jm_send_user', group_id=str(msg.group_id)):
                        await bot.api.post_private_msg(msg.user_id,text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送喵~")
                        await bot.api.upload_private_file(msg.user_id, pdf_path, f"{comic_id}.pdf")
                    else:
                        await msg.reply(text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送到群组喵~")
                        await bot.api.post_group_file(msg.group_id, file=pdf_path)
                else:
                    await msg.reply(text="群组发送漫画已关闭喵~")
            else:
                if switch.get_switch_state('jm_send', user_id=str(msg.user_id)):
                    await bot.api.post_private_msg(msg.user_id,text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送喵~")
                    await bot.api.upload_private_file(msg.user_id, pdf_path, f"{comic_id}.pdf")
                else:
                    await msg.reply(text="该漫画已下载，但用户私信发送漫画已关闭喵~")
            return

        if int(comic_id) <= len(comic_cache) and len(comic_cache) > 0 :
            try:
                comic_id = comic_cache[int(comic_id)-1]
            except IndexError:
                error_msg = "超出范围了喵~"
                if is_group:
                    await msg.reply(text=error_msg)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=error_msg)
                return
        
        try:
            client = JmOption.default().new_jm_client()
        except JmcomicException:
            error_msg = "当前禁漫站点接口不可用喵~ 可能是 /setting 接口返回异常，请稍后重试或检查jmcomic配置喵~"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
            return
        try:
            album: JmAlbumDetail = client.get_album_detail(comic_id)
        except MissingAlbumPhotoException:
            error_msg = "该漫画ID不存在喵~"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
            return
        
        # 立即回复用户，不等待下载完成
        reply_text = f"已开始下载漫画ID：{comic_id}，下载完成后会自动通知喵~"
        if is_group:
            await msg.reply(text=reply_text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply_text)

        # 创建后台任务
        try:
            await asyncio.gather(download_and_send_comic(comic_id, msg, is_group))
        except Exception as e:
            error_msg = f"下载漫画失败喵~: {str(e)}"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)

    else:
        error_msg = "格式错误了喵~，请输入 /jm 后跟漫画ID"
        if not is_group:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

#后台任务函数
async def download_and_send_comic(comic_id, msg, is_group):
    try:
        # 在线程池中执行阻塞操作
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda:
            jmcomic.download_album(
                comic_id,
                jmcomic.create_option_by_file('./resources/config/option.yml')
            )
        )

        file_path = normalize_file_path(os.path.join(load_address(), f"pdf/{comic_id}.pdf"))

        # 检查文件是否真正生成
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"PDF文件未生成：{file_path}")
        encrypt_needed = switch.get_switch_state('pdf_password', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None)
        if switch.get_switch_state('jm_send_email', user_id=str(msg.user_id)):
            encrypt_needed = True
        if encrypt_needed:
            try:
                import pikepdf
                with pikepdf.open(file_path,allow_overwriting_input=True) as pdf:
                    pdf.save(file_path, encryption=pikepdf.Encryption(
                        owner=comic_id,
                        user=comic_id,
                        R=4
                    ))
            except ImportError:
                error_msg = "缺少pikepdf库，无法加密PDF文件喵~"
                await msg.reply(text=error_msg)
        email_sent = False
        email_error = None
        try:
            if switch.get_switch_state('jm_send_email', user_id=str(msg.user_id)):
                email_sent = await send_comic_email(str(msg.user_id), comic_id, file_path)
        except Exception as e:
            _log.error(f"发送漫画邮件失败: {e}")
            email_error = e
        if not switch.get_switch_state('jm_send', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None):
            text = "漫画已下载，但发送已关闭喵~"
            if email_sent:
                text = "漫画已下载，并已发送到你的邮箱喵~"
            elif email_error is not None:
                err_msg = str(email_error)
                if "552" in err_msg or "mailsize limit" in err_msg.lower():
                    text = "漫画已下载，但发送到邮箱失败喵~，原因是邮件大小超过邮箱限制喵~"
                else:
                    text = "漫画已下载，但发送到邮箱失败喵~，请检查邮箱配置或稍后重试喵~"
            if is_group:
                await msg.reply(text=text)
            else:
                await bot.api.post_private_msg(msg.user_id, text=text)
            return

        file_size = os.path.getsize(file_path) / (1024 * 1024)  # 转换为MB
        file_text = f"文件大小：{file_size:.2f} MB，正在上传喵~"
        success_text = f"漫画 {comic_id} 下载完成喵~"

        if is_group:
            if switch.get_switch_state('jm_send_user', group_id=str(msg.group_id)):
                await bot.api.post_private_msg(msg.user_id, text=file_text)
                await bot.api.upload_private_file(msg.user_id, file_path, f"{comic_id}.pdf")
                await bot.api.post_private_msg(msg.user_id, text=success_text)
            else:
                await msg.reply(text=file_text)
                await bot.api.post_group_file(msg.group_id, file=file_path)
                await msg.reply(text=success_text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=file_text)
            await bot.api.upload_private_file(msg.user_id, file_path, f"{comic_id}.pdf")
            await bot.api.post_private_msg(msg.user_id, text=success_text)

    except Exception as e:
        file_path = normalize_file_path(os.path.join(load_address(), f"pdf/{comic_id}.pdf"))
        error_msg = f"下载失败喵~: {str(e)}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)
        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path) / (1024 * 1024)  # 转换为MB
            file_text = f"文件大小：{file_size:.2f} MB，正在上传喵~"
            if is_group:
                await msg.reply(text="部分下载失败了喵~，正在发送剩余的文件喵~\n"+file_text)
                await bot.api.post_group_file(msg.group_id, file=file_path)
            else:
                await bot.api.post_private_msg(msg.user_id, text="部分下载失败了喵~，正在发送剩余的文件喵~\n"+file_text)
                await bot.api.upload_private_file(msg.user_id, file_path, f"{comic_id}.pdf")

@register_command("/jm_clear",help_text = "/jm_clear -> 清除缓存",category = "1")
async def handle_jm_clear(msg, is_group=True):
    comic_cache.clear()
    if is_group:
        await msg.reply(text="缓存已清除喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="缓存已清除喵~")

@register_command("/jm_send_user", help_text="/jm_send_user <on|off> -> 开启/关闭群聊用户私信发送漫画(admin)",category = "1",admin_show=True)
async def handle_jm_send_user(msg, is_group=True):
    if str(msg.user_id) not in admin:
        await msg.reply(text="只有管理员才能使用该命令喵~")
        return

    state = msg.raw_message[len("/jm_send_user"):].strip().lower()
    if state not in ['on', 'off']:
        reply = "请输入 on 或 off 喵~"
    else:
        switch.set_switch_state('jm_send_user', state == 'on', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None)

        reply = f"用户私信发送漫画已 {'开启' if state == 'on' else '关闭'} 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)
    switch.save_switches()

@register_command("/jm_send", help_text="/jm_send <on|off> -> 开启/关闭发送漫画(admin)",category = "1",admin_show=True)
async def handle_jm_send(msg, is_group=True):
    if str(msg.user_id) not in admin:
        await msg.reply(text="只有管理员才能使用该命令喵~")
        return
    state = msg.raw_message[len("/jm_send"):].strip().lower()
    if state not in ['on', 'off']:
        reply = "请输入 on 或 off 喵~"
    else:
        switch.set_switch_state('jm_send', state == 'on', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None)

        reply = f"{'群组' if is_group else '用户'}发送漫画已 {'开启' if state == 'on' else '关闭'} 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)
    switch.save_switches()

@register_command("/jm_pwd", help_text="/jm_pwd <on|off> -> 开启/关闭密码加密(admin)，密码为漫画id",category = "1",admin_show=True)

async def handle_jm_pwd(msg, is_group=True):
    if str(msg.user_id) not in admin:
        await msg.reply(text="只有管理员才能使用该命令喵~")
        return
    state = msg.raw_message[len("/jm_pwd"):].strip().lower()
    if state not in ['on', 'off']:
        reply = "请输入 on 或 off 喵~"
    else:
        switch.set_switch_state('pdf_password', state == 'on', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None)
        reply = f"{'群组' if is_group else '用户'}密码加密已 {'开启' if state == 'on' else '关闭'} 喵~，密码为漫画id"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/jm_email", help_text="/jm_email <邮箱> <on|off> -> 配置邮箱并开启或关闭发送漫画到邮箱",category = "1")
async def handle_jm_email(msg, is_group=True):
    user_id = str(msg.user_id)
    raw = msg.raw_message[len("/jm_email"):].strip()
    parts = raw.split() if raw else []
    email = None
    state = None
    if not parts:
        current_email = user_email.get(user_id)
        enabled = switch.get_switch_state('jm_send_email', user_id=user_id)
        text = f"当前邮箱：{current_email or '未设置'}，状态：{'开启' if enabled else '关闭'}"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    if len(parts) == 1:
        if parts[0].lower() in ("on", "off"):
            state = parts[0].lower() == "on"
        else:
            email = parts[0]
            state = True
    else:
        email = parts[0]
        if parts[1].lower() in ("on", "off"):
            state = parts[1].lower() == "on"
        else:
            text = "第二个参数请输入 on 或 off 喵~"
            if is_group:
                await msg.reply(text=text)
            else:
                await bot.api.post_private_msg(msg.user_id, text=text)
            return
    if email:
        if "@" not in email:
            text = "请输入正确的邮箱地址喵~"
            if is_group:
                await msg.reply(text=text)
            else:
                await bot.api.post_private_msg(msg.user_id, text=text)
            return
        user_email[user_id] = email
        save_email_config()
    if state is not None:
        switch.set_switch_state('jm_send_email', state, user_id=user_id)
        switch.save_switches()
    text = "邮箱配置已更新喵~"
    if state is not None:
        text = f"邮箱配置已更新喵~，发送到邮箱已{'开启' if state else '关闭'}"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

# ====下面的收藏夹不是官方的收藏夹，是本地储存的====
@register_command("/add_fav", help_text="/add_fav <漫画ID> -> 添加收藏",category = "1")
async def handle_add_favorite(msg, is_group=True):
    comic_id = msg.raw_message[len("/add_fav"):].strip()
    if not comic_id.isdigit():
        reply = "请输入有效的漫画ID喵~"
    else:
        user_id = str(msg.user_id)
        if is_group:
            group_id = str(msg.group_id)
            if group_id not in group_favorites:
                group_favorites[group_id] = {}
            if user_id not in group_favorites[group_id]:
                group_favorites[group_id][user_id] = []
            if comic_id not in group_favorites[group_id][user_id]:
                group_favorites[group_id][user_id].append(comic_id)
                reply = f"已在群组收藏中添加漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 已在群组收藏中喵~"
        else:
            if user_id not in user_favorites:
                user_favorites[user_id] = []
            if comic_id not in user_favorites[user_id]:
                user_favorites[user_id].append(comic_id)
                reply = f"已在个人收藏中添加漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 已在个人收藏中喵~"
        save_favorites()
    
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/list_fav", help_text="/list_fav -> 查看收藏列表",category = "1")
async def handle_list_favorites(msg, is_group=True):
    user_id = str(msg.user_id)
    if is_group:
        group_id = str(msg.group_id)
        if group_id in group_favorites and user_id in group_favorites[group_id]:
            comics = group_favorites[group_id][user_id]
        else:
            comics = []
    else:
        comics = user_favorites.get(user_id, [])
    
    if comics:
        reply = "收藏的漫画ID:\n" + "\n".join(comics)
    else:
        reply = "收藏夹是空的喵~"
    
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/del_fav", help_text="/del_fav <漫画ID> -> 删除收藏",category = "1")
async def handle_del_favorite(msg, is_group=True):
    comic_id = msg.raw_message[len("/del_fav"):].strip()
    user_id = str(msg.user_id)
    if is_group:
        group_id = str(msg.group_id)
        if group_id in group_favorites and user_id in group_favorites[group_id]:
            if comic_id in group_favorites[group_id][user_id]:
                group_favorites[group_id][user_id].remove(comic_id)
                reply = f"已从群组收藏中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在群组收藏中喵~"
        else:
            reply = "群组收藏夹是空的喵~"
    else:
        if user_id in user_favorites:
            if comic_id in user_favorites[user_id]:
                user_favorites[user_id].remove(comic_id)
                reply = f"已从个人收藏中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在个人收藏中喵~"
        else:
            reply = "个人收藏夹是空的喵~"
    
    save_favorites()
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/add_black_list","/abl",help_text = "/add_black_list 或 /abl  <漫画ID> -> 添加黑名单",category = "1")
async def handle_add_black_list(msg, is_group=True):
    comic_id = ""
    if msg.raw_message.startswith("/add_black_list"):
        comic_id = msg.raw_message[len("/add_black_list"):].strip()
    elif msg.raw_message.startswith("/abl"):
        comic_id = msg.raw_message[len("/abl"):].strip()

    if not comic_id.isdigit():
        reply = "请输入有效的漫画ID喵~"
    else:
        if is_group:
            group_id = str(msg.group_id)
            if group_id not in black_list_comic["groups"]:
                black_list_comic["groups"][group_id] = []
            if comic_id in black_list_comic["groups"][group_id]:
                reply = f"漫画 {comic_id} 已在本群黑名单中喵~"
            else:
                black_list_comic["groups"][group_id].append(comic_id)
                write_blak_list()
                reply = f"已在本群黑名单中添加漫画 {comic_id} 喵~"
        else:
            user_id = str(msg.user_id)
            if user_id not in black_list_comic["users"]:
                black_list_comic["users"][user_id] = []
            if comic_id in black_list_comic["users"][user_id]:
                reply = f"漫画 {comic_id} 已在你的黑名单中喵~"
            else:
                black_list_comic["users"][user_id].append(comic_id)
                write_blak_list()
                reply = f"已在你的黑名单中添加漫画 {comic_id} 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/add_global_black_list","/agbl",help_text = "/add_global_black_list 或 /agbl <漫画ID> -> 添加全局黑名单(admin)",category = "1",admin_show=True)
async def handle_add_global_black_list(msg, is_group=True):

    if str(msg.user_id) not in admin:
        reply = "你没有权限喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    comic_id = ""
    if msg.raw_message.startswith("/add_global_black_list"):
        comic_id = msg.raw_message[len("/add_global_black_list"):].strip()
    elif msg.raw_message.startswith("/agbl"):
        comic_id = msg.raw_message[len("/agbl"):].strip()

    if not comic_id.isdigit():
        reply = "请输入有效的漫画ID喵~"
    else:
        if comic_id in black_list_comic["global"]:
            reply = f"漫画 {comic_id} 已在全局黑名单中喵~"
        else:
            black_list_comic["global"].append(comic_id)
            write_blak_list()
            reply = f"已在全局黑名单中添加漫画 {comic_id} 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/del_global_black_list","/dgbl",help_text = "/del_global_black_list 或 /dgbl <漫画ID> -> 删除全局黑名单(admin)",category = "1",admin_show=True)
async def handle_del_global_black_list(msg, is_group=True):

    if str(msg.user_id) not in admin:
        reply = "你没有权限喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return
    
    comic_id = ""
    if msg.raw_message.startswith("/del_global_black_list"):
        comic_id = msg.raw_message[len("/del_global_black_list"):].strip()
    elif msg.raw_message.startswith("/dgbl"):
        comic_id = msg.raw_message[len("/dgbl"):].strip()

    if not comic_id.isdigit():
        reply = "请输入有效的漫画ID喵~"
    else:
        if comic_id in black_list_comic["global"]:
            black_list_comic["global"].remove(comic_id)
            write_blak_list()
            reply = f"已从全局黑名单中删除漫画 {comic_id} 喵~"
        else:
            reply = f"漫画 {comic_id} 不在全局黑名单中喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/del_black_list","/dbl",help_text = "/del_black_list 或 /dbl <漫画ID> -> 删除黑名单",category = "1")
async def handle_del_black_list(msg, is_group=True):
    comic_id = ""
    if msg.raw_message.startswith("/del_black_list"):
        comic_id = msg.raw_message[len("/del_black_list"):].strip()
    elif msg.raw_message.startswith("/dbl"):
        comic_id = msg.raw_message[len("/dbl"):].strip()

    if not comic_id.isdigit():
        reply = "请输入有效的漫画ID喵~"
    else:
        if is_group:
            group_id = str(msg.group_id)
            if group_id in black_list_comic["groups"] and comic_id in black_list_comic["groups"][group_id]:
                black_list_comic["groups"][group_id].remove(comic_id)
                write_blak_list()
                reply = f"已从本群黑名单中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在本群黑名单中喵~"
        else:
            user_id = str(msg.user_id)
            if user_id in black_list_comic["users"] and comic_id in black_list_comic["users"][user_id]:
                black_list_comic["users"][user_id].remove(comic_id)
                await write_blak_list()
                reply = f"已从你的黑名单中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在你的黑名单中喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/list_black_list","/lbl",help_text = "/list_black_list 或 /lbl -> 查看黑名单",category = "1")
async def handle_list_black_list(msg, is_group=True):
    if is_group:
        group_id = str(msg.group_id)
        if black_list_comic["global"] or black_list_comic["groups"].get(group_id, []):
            reply = "本群的黑名单中的漫画ID:\n全局：" + "\n".join( black_list_comic["global"]) + "\n本群：" + "\n".join( black_list_comic["groups"].get(group_id, []))
        else:
            reply = "本群黑名单是空的喵~"
    else:
        user_id = str(msg.user_id)
        if black_list_comic["global"] or black_list_comic["users"].get(user_id, []):
            reply = "你的黑名单中的漫画ID:\n全局：" + "\n".join(black_list_comic["global"]) + "\n个人：" + "\n".join(black_list_comic["users"].get(user_id, []))
        else:
            reply = "你的黑名单是空的喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)
        
#------------------------

@register_command("/set_prompt","/sp",help_text = "/set_prompt 或者 /sp <提示词> -> 设定提示词(仅群admin)",category = "2")
async def handle_set_prompt(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        reply = "你没有权限喵~"
        await msg.reply(text=reply)
        return

    prompt_content = ""
    if msg.raw_message.startswith("/set_prompt"):
        prompt_content = msg.raw_message[len("/set_prompt"):].strip()
    elif msg.raw_message.startswith("/sp"):
        prompt_content = msg.raw_message[len("/sp"):].strip()
    id_str = str(msg.group_id if is_group else msg.user_id)
    os.makedirs("resources/prompts/group", exist_ok=True)
    os.makedirs("resources/prompts/user", exist_ok=True)

    prefix = "group" if is_group else "user"
    with open(f"resources/prompts/{prefix}/{prefix}_{id_str}.txt", "w", encoding="utf-8") as file:
        file.write(prompt_content)

    messages = group_messages if is_group else user_messages
    if id_str in messages:
        del messages[id_str]
    messages[id_str] = [{"role": "system", "content": prompt_content}]

    reply_text = "群组提示词已更新，对话记录已清除喵~" if is_group else "个人提示词已更新，对话记录已清除喵~"
    if is_group:
        await msg.reply(text=reply_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply_text)


@register_command("/del_prompt","/dp",help_text = "/del_prompt 或者 /dp -> 删除提示词(仅群admin)",category = "2")
async def handle_del_prompt(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        reply = "你没有权限喵~"
        await msg.reply(text=reply)
        return

    id_str = str(msg.group_id if is_group else msg.user_id)
    if is_group:
        if id_str in group_messages:
            del group_messages[id_str]
            try:
                os.remove(f"resources/prompts/group/group_{id_str}.txt")
                with open("resources/prompts/neko.txt", "r", encoding="utf-8") as file:
                    prompt = file.read()
                    group_messages[id_str] = [{"role": "system", "content": prompt}]
                await msg.reply(text="提示词已删除喵~本子娘回来了喵~")
            except FileNotFoundError:
                await msg.reply(text="没有可以删除的提示词喵~")

    else:
        if id_str in user_messages:
            del user_messages[id_str]
            try:
                os.remove(f"resources/prompts/user/user_{id_str}.txt")
                with open("resources/prompts/neko.txt", "r", encoding="utf-8") as file:
                    prompt = file.read()
                    user_messages[id_str] = [{"role": "system", "content": prompt}]
                await bot.api.post_private_msg(msg.user_id, text="提示词已删除喵~本子娘回来了喵~")
            except FileNotFoundError:
                await bot.api.post_private_msg(msg.user_id, text="没有可以删除的提示词喵~")

@register_command("/get_prompt","/gp",help_text = "/get_prompt 或者 /gp -> 获取提示词(仅群admin)",category = "2")
async def handle_get_prompt(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        reply = "你没有权限喵~"
        await msg.reply(text=reply)
        return

    id_str = str(msg.group_id if is_group else msg.user_id)
    if is_group:
        try:
            with open(f"resources/prompts/group/group_{id_str}.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
                await msg.reply(text=prompt)
        except FileNotFoundError:
            await msg.reply(text="没有找到提示词喵~")
    else:
        try:
            with open(f"resources/prompts/user/user_{id_str}.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
                await bot.api.post_private_msg(msg.user_id, text=prompt)
        except FileNotFoundError:
            await bot.api.post_private_msg(msg.user_id, text="没有找到提示词喵~")


@register_command("/agree",help_text="/agree -> 同意好友请求(admin)",category = "4",admin_show=True) # 同意好友请求
async def handle_agree(msg, is_group=True):
    if str(msg.user_id) not in admin:
        reply = "你没有权限喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    if not is_group:
        await bot.api.set_friend_add_request(flag=msg.user_id, approve=True,remark=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text="已同意好友请求喵~")
    else:
        await bot.api.set_friend_add_request(flag=msg.user_id, approve=True,remark=msg.user_id)
        await msg.reply(text="已同意好友请求喵~")

@register_command("/restart",help_text="/restart -> 重启机器人(admin)",category = "4",admin_show=True)
async def handle_restart(msg, is_group=True):
    if str(msg.user_id) not in admin:
        if is_group:
            await msg.reply(text="只有管理员才能重启机器人喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="只有管理员才能重启机器人喵~")
        return
    reply_text = "正在重启喵~"
    if is_group:
        await msg.reply(text=reply_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply_text)
    # 重启逻辑
    os.execv(sys.executable, [sys.executable] + sys.argv)

@register_command("/shutdown",help_text="/shutdown -> 关闭机器人(admin)",category = "4",admin_show=True)
async def handle_shutdown(msg, is_group=True):
    if str(msg.user_id) not in admin:
        if is_group:
            await msg.reply(text="只有管理员才能关闭机器人喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="只有管理员才能关闭机器人喵~")
        return
    reply_text = "主人，下次再见喵~"
    if is_group:
        await msg.reply(text=reply_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply_text)
    import sys
    sys.exit()

#------以下为调用api发送文件的命令，采用异步方式发送文件------
# 新增后台任务函数
async def async_send_file(is_group,send_method, target_id, file_type, url,file_name):
    try:
        # 处理可能的重定向
        loop = asyncio.get_event_loop()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = await loop.run_in_executor(None, lambda: requests.get(url, allow_redirects=True, timeout=10,headers=headers))
        final_url = response.url

        # 异步发送文件
        if is_group:
            await send_method(target_id, **{file_type: final_url})
        else:
            await send_method(target_id, **{file_type: final_url})
    except Exception as e:
        error_msg = f"发送失败喵~: {str(e)}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(target_id, text=error_msg)

def _send_comic_email_sync(to_addr, subject, body, file_path, conf):
    if not conf:
        raise ValueError("smtp未配置")
    host = conf.get("host")
    port = int(conf.get("port", 587))
    user = conf.get("user")
    password = conf.get("password")
    use_tls = bool(conf.get("use_tls", True))
    from_addr = conf.get("from_addr") or user
    if not host or not from_addr:
        raise ValueError("smtp配置不完整")
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with open(file_path, "rb") as f:
        part = MIMEBase("application", "pdf")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(file_path)}"')
    msg.attach(part)
    server = smtplib.SMTP(host, port, timeout=30)
    if use_tls:
        server.starttls()
    if user and password:
        server.login(user, password)
    server.sendmail(from_addr, [to_addr], msg.as_string())
    server.quit()

async def send_comic_email(user_id, comic_id, file_path):
    uid = str(user_id)
    to_addr = user_email.get(uid)
    if not to_addr:
        return False
    conf = smtp_config.get(uid) or smtp_config.get("global")
    if not conf:
        return False
    subject = f"漫画 {comic_id}"
    body = f"漫画 {comic_id} 已发送喵~"
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _send_comic_email_sync(to_addr, subject, body, file_path, conf))
    return True
            
# 修改通用处理函数
async def handle_generic_file(msg, is_group: bool, section: str, file_type: str, custom_url: str = None, file_name:str = None,custom_send_method=None):
    """通用文件处理函数
       :param msg: 消息对象
       :param is_group: 是否为群组消息
       :param section: 配置文件中的section名称(可选)
       :param file_type: 文件类型(image、record、video、file、markdown)
       :param custom_url: 自定义URL(可选)
       :param file_name: 文件名(可选)
       :param custom_send_method: 发送方法(可选)
    """
    """
        支持的file_type:
        image: 图片
        record: 语音
        video: 视频
        file: 文件
        markdown: Markdown
    """

    try:
        # 修复配置读取逻辑
        if section:  # 仅当需要读取配置文件时
            loop = asyncio.get_event_loop()
            # 正确读取配置的方式
            def read_config():
                cfg = configparser.ConfigParser()
                cfg.read('resources/config/urls.ini')
                if not cfg.has_section(section):
                    raise Exception(f"配置文件中缺少 [{section}] 段落")
                return cfg

            config = await loop.run_in_executor(None, read_config)
            urls = json.loads(config.get(section, 'urls'))
            selected_url = random.choice(urls)
        else:  # 使用自定义URL
            selected_url = custom_url

        # 创建后台任务
        send_method = bot.api.post_group_file if is_group else bot.api.post_private_file
        target_id = msg.group_id if is_group else msg.user_id
        if custom_send_method:
            await async_send_file(is_group,custom_send_method, target_id, file_type, selected_url,file_name)
        else:
            asyncio.create_task(
                async_send_file(is_group,send_method, target_id, file_type, selected_url,file_name)
            )

    except Exception as e:
        error_msg = f"配置错误喵~: {str(e)}" if '配置' in str(e) else f"获取失败喵~: {str(e)}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

# 统一调用
@register_command("/random_image","/ri",help_text = "/random_image 或者 /ri -> 随机图片",category = "3")
async def handle_random_image(msg, is_group=True):
    await handle_generic_file(msg, is_group, 'ri', 'image')

@register_command("/random_emoticons","/re",help_text = "/random_emoticons 或者 /re -> 随机表情包",category = "3")
async def handle_random_emoticons(msg, is_group=True):
    await handle_generic_file(msg, is_group, 're', 'image')

@register_command("/st",help_text = "/st <标签名> -> 发送随机涩图,标签支持与或(& |)",category = "3")
async def handle_st(msg, is_group=True):
    tags = msg.raw_message[len("/st"):].strip()
    res = requests.get(f"https://api.lolicon.app/setu/v2?tag={tags}").json().get("data")[0].get("urls").get("original")
    await handle_generic_file(msg, is_group,"","image",custom_url=res)  # 特殊处理API调用

@register_command("/random_video","/rv",help_text = "/random_video 或者 /rv -> 随机二次元视频",category = "3")
async def handle_random_video(msg, is_group=True):
    await handle_generic_file(msg, is_group, 'rv', 'video')

@register_command("/dv",help_text="/dv <link> -> 下载视频",category = "3")
async def handle_d(msg, is_group=True):
    link = msg.raw_message[len("/dv"):].strip()
    if not link:
        if is_group:
            await msg.reply(text="请输入链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入链接喵~")
        return

    if re.match(r'^https?://', link):  # 检查是否为合法链接
        await handle_generic_file(msg, is_group, '', 'video', custom_url=link)  
    else:
        if is_group:
            await msg.reply(text="请输入合法的链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入合法的链接喵~")

@register_command("/di",help_text="/di <link> -> 下载图片",category = "3")
async def handle_di(msg, is_group=True):
    link = msg.raw_message[len("/di"):].strip()
    if not link:
        if is_group:
            await msg.reply(text="请输入链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入链接喵~")
        return

    if re.match(r'^https?://', link):  # 检查是否为合法链接
        #await handle_generic_file(msg, is_group, '', 'image', custom_url=link,file_name="download.jpg")
        if is_group:
            await bot.api.post_group_file(group_id=msg.group_id,file=link)
        else:
            await bot.api.upload_private_file(user_id=msg.user_id,file=link,name="download.jpg")
    else:
        if is_group:
            await msg.reply(text="请输入合法的链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入合法的链接喵~")

@register_command("/df",help_text="/df <link> -> 下载文件",category = "3")
async def handle_df(msg, is_group=True):
    link = msg.raw_message[len("/df"):].strip()
    if not link:
        if is_group:
            await msg.reply(text="请输入链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入链接喵~")
        return

    if re.match(r'^https?://', link):  # 检查是否为合法链接
        await handle_generic_file(msg, is_group, '', 'file', custom_url=link)
    else:
        if is_group:
            await msg.reply(text="请输入合法的链接喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="请输入合法的链接喵~")

#---------------------------------------------

@register_command("/music","/m",help_text = "/music <音乐名/id> -> 发送音乐",category = "3")
async def handle_music(msg, is_group=True):
    music_name = msg.raw_message[len("/music"):].strip()
    if not music_name:
        await msg.reply(text="请输入音乐名喵~")
        return

    if re.match(r'^\d+$', music_name):  # 检查是否为纯数字
        messagechain = MessageChain(
            Music(type="163",id=music_name)
        )
        if is_group:
            await msg.reply(rtf=messagechain)
        else:
            await bot.api.post_private_msg(msg.user_id, rtf=messagechain)
        return
    music_id = None
    url = 'https://music.163.com/api/search/get'
    params = {
        's': music_name,
        'type': 1,  # 1表示歌曲
        'limit': 1  # 获取第一条结果
    }
    response = requests.get(url, params=params)
    data = response.json()
    if data['code'] == 200 and data['result']['songs']:
        music_id = data['result']['songs'][0]['id']
    messagechain = MessageChain(
        Music(type="163",id=music_id)
    )
    if is_group:
        await bot.api.post_group_msg(msg.group_id, rtf=messagechain)
    else:
        await bot.api.post_private_msg(msg.user_id, rtf=messagechain)

@register_command("/random_music","/rm",help_text = "/random_music 或者 /rm -> 发送随机音乐",category = "3")
async def handle_random_music(msg, is_group=True):
    id = requests.get("https://api.mtbbs.top/Music/song/?id=2645495145").json()["data"]["id"]
    messagechain = MessageChain(
        Music(type="163",id=id)
    )
    if is_group:
        await bot.api.post_group_msg(msg.group_id, rtf=messagechain)
    else:
        await bot.api.post_private_msg(msg.user_id, rtf=messagechain)

@register_command("/random_dice","/rd",help_text = "/random_dice 或者 /rd -> 发送随机骰子",category = "3")
async def handle_random_dice(msg, is_group=True):
    if is_group:
        await bot.api.post_group_msg(msg.group_id,dice=True)
    else:
        await bot.api.post_private_msg(msg.user_id,dice=True)

@register_command("/random_rps","/rps",help_text = "/random_rps 或者 /rps -> 发送随机石头剪刀布",category = "3")
async def handle_random_rps(msg, is_group=True):
    if is_group:
        await bot.api.post_group_msg(msg.group_id,rps=True)
    else:
        await bot.api.post_private_msg(msg.user_id,rps=True)

@register_command("/del_message","/dm",help_text = "/del_message 或者 /dm -> 删除对话记录(仅群admin)",category = "3")
async def handle_del_message(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        await msg.reply(text="你没有权限喵~")
        return

    if is_group:
        try:
            del group_messages[str(msg.group_id)]
        except KeyError:
            await msg.reply(text="你没有对话记录喵~")
            return
        with open("saved_message/group_messages.json", "w", encoding="utf-8") as f:
            json.dump(group_messages, f, ensure_ascii=False, indent=4)
        # 删除对应的工作区
        delete_session_workspace(group_id=str(msg.group_id), group_user_id=str(msg.user_id))
        await msg.reply(text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")
    else:
        try:
            del user_messages[str(msg.user_id)]
        except KeyError:
            await bot.api.post_private_msg(msg.user_id, text="你没有对话记录喵~")
            return
        with open("saved_message/user_messages.json", "w", encoding="utf-8") as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        # 删除对应的工作区
        delete_session_workspace(user_id=str(msg.user_id))
        await bot.api.post_private_msg(msg.user_id, text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")

@register_command("/remind",help_text="/remind <多少小时后> <内容> -> 定时提醒",category = "7")
async def handle_remind(msg, is_group=True):
    match = re.match(r'^/remind\s+(\d+\.?\d*)\s+(.+)$', msg.raw_message) #正则支持小数
    if match:
        hours = float(match.group(1))
        content = match.group(2)
    else:
        if is_group:
            await msg.reply(text="格式错误喵~ 请输入 /remind 时间(小时) 内容")
            return
        else:
            await bot.api.post_private_msg(msg.user_id, text="格式错误喵~ 请输入 /remind 时间(小时) 内容")
            return
    if is_group:
        await msg.reply(text=f"已设置提醒喵~{hours}小时后会提醒你：{content}")
        asyncio.create_task(schedule_task(hours, msg.reply,content))
    else:
        await bot.api.post_private_msg(msg.user_id, text=f"已设置提醒喵~{hours}小时后会提醒你：{content}")
        asyncio.create_task(schedule_task(hours, bot.api.post_private_msg,msg.user_id,content))

@register_command("/premind", help_text="/premind <MM-DD> <HH:MM> <内容> -> 精确时间提醒",category = "7")
async def handle_precise_remind(msg, is_group=True):
    try:
        # 解析日期时间
        parts = msg.raw_message.split(maxsplit=3)

        if len(parts) < 3:
            raise ValueError
        
        now = datetime.now()
        year = str(now.year)

        date_str = f"{year}-" + parts[1]
        time_str = parts[2]
        content = parts[3] if len(parts) > 3 else "提醒时间到了喵~"
        
        target_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        if target_time < now:
            if is_group:
                await msg.reply(text="时间已经过去喵~")
            else:
                await bot.api.post_private_msg(msg.user_id, text="时间已经过去喵~")
            return

        reply = f"已设置精确提醒喵~将在 {target_time} 提醒: {content}"
        if is_group:
            await msg.reply(text=reply)
            asyncio.create_task(schedule_task_by_date(target_time, msg.reply, content))
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
            asyncio.create_task(schedule_task_by_date(target_time, bot.api.post_private_msg, msg.user_id, content))
            
    except ValueError:
        error_msg = "格式错误喵~ 使用: /precise_remind MM-DD HH:MM 提醒内容"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

@register_command("/smtp", help_text="/smtp <host> <port> <user> <password> <tls(1/0)> <from> -> 配置当前用户SMTP服务",category = "4")
async def handle_smtp_config_command(msg, is_group=True):
    user_id = str(msg.user_id)
    raw = msg.raw_message[len("/smtp"):].strip()
    parts = raw.split() if raw else []
    if not parts:
        conf = smtp_config.get(user_id) or smtp_config.get("global")
        if conf:
            text = f"当前SMTP已配置喵~ host={conf.get('host')}, port={conf.get('port')}"
        else:
            text = "当前还没有配置SMTP喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    if len(parts) < 5:
        text = "格式错误喵~ 应为: /smtp host port user password tls(1/0) [from]"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError:
        text = "端口必须是数字喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    user = parts[2]
    password = parts[3]
    use_tls = parts[4] != "0"
    from_addr = parts[5] if len(parts) > 5 else user
    smtp_config[user_id] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "use_tls": use_tls,
        "from_addr": from_addr
    }
    save_smtp_config()
    text = "SMTP配置已更新喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/task",help_text="/task </bot.api.xxxx(参数1=值1...)> <时间(小时)> <是否循环(1/0)> -> 设置定时任务(admin)",category = "7",admin_show=True)
async def handle_task(msg,is_group=True):
    if str(msg.user_id) not in admin:
        text = "你没有权限设置定时任务喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    
    match = re.match(r'^/task\s+(.+)\s+(\d+\.?\d*)\s+(\d)$', msg.raw_message) #正则支持小数
    if match:
        command_str = match.group(1)
        hours = float(match.group(2))
        loop = int(match.group(3))
    else:
        error_msg = "格式错误喵~ 请输入 /task bot.api.xxxx(参数1=值1...) 时间(小时) 是否循环(1/0)"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg) 
        return
    if loop not in [0,1]:
        error_msg = "格式错误喵~ 请输入 /task bot.api.xxxx(参数1=值1...) 时间(小时) 是否循环(1/0)"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg) 
        return
    
    dict = parse_command_string(command_str)
    command = dict["func"]
    params = dict["params"]
    try:
        func = getattr(bot.api, command.split('.')[-1])
    except Exception as e:
        error_msg = f"发生错误喵~ 请检查命令是否正确。{e}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg) 
        return

    if loop == 0:
        task = asyncio.create_task(schedule_job_task(hours,0,f"{command_str}_{hours}_{loop}",func, **params))
        schedule_tasks[f"{command_str}_{hours}_{loop}"] = task
        if is_group:
            await msg.reply(text=f"已设置定时任务喵~{hours}小时后会执行：{command_str}")
        else:
            await bot.api.post_private_msg(msg.user_id, text=f"已设置定时任务喵~{hours}小时后会执行：{command_str}")
        return

    else:
        task = asyncio.create_task(schedule_job_task(hours,1,f"{command_str}_{hours}_{loop}",func, **params))
        schedule_tasks[f"{command_str}_{hours}_{loop}"] = task
        if is_group:
            await msg.reply(text=f"已设置循环定时任务喵~{hours}小时后会执行：{command_str}")
        else:
            await bot.api.post_private_msg(msg.user_id, text=f"已设置循环定时任务喵~{hours}小时后会执行：{command_str}")
        return

@register_command("/list_tasks","/lt",help_text = "/list_tasks 或者 /lt -> 查看定时任务(admin)",category = "7",admin_show=True)
async def handle_list_tasks(msg, is_group=True):
    if str(msg.user_id) not in admin:
        text = "你没有权限查看定时任务喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    text = "定时任务列表：\n"
    tot = 0
    for i in schedule_tasks.keys():
        tot += 1
        text += f"{tot}. {i}\n"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)
    return

@register_command("/cancel_tasks","/ct",help_text = "/cancel_tasks 或者 /ct <任务名> -> 取消定时任务(admin)",category = "7",admin_show=True)
async def handle_cancel_tasks(msg, is_group=True):
    if str(msg.user_id) not in admin:
        text = "你没有权限取消定时任务喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    pre = "/cancel_tasks" if msg.raw_message.startswith("/cancel_tasks") else "/ct"
    name = msg.raw_message[len(pre):].strip()

    if name == "":
        text = "请输入任务名喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    
    if name not in schedule_tasks:
        text = "没有这个任务喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    
    schedule_tasks[name].cancel()
    del schedule_tasks[name]
    text = "取消成功喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)
    return

@register_command("/set_admin","/sa",help_text = "/set_admin <qq号> 或者 /sa <qq号> -> 设置管理员(root)",category = "4",admin_show=True)
async def handle_set_admin(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return
    if str(msg.user_id) != str(admin_id):
        await bot.api.post_private_msg(msg.user_id, text="你没有权限设置管理员喵~")
        return

    if msg.raw_message.startswith("/set_admin"):
        id = msg.raw_message[len("/set_admin"):].strip()
    else: 
        id = msg.raw_message[len("/sa"):].strip()

    if id in admin:
        await bot.api.post_private_msg(msg.user_id, text="已经是管理员了喵~")
        return

    admin.append(id)
    write_admin()
    await bot.api.post_private_msg(msg.user_id, text="设置成功喵~，现在"+id+"是管理员喵~")

@register_command("/del_admin","/da",help_text = "/del_admin <qq号> 或者 /da <qq号> -> 删除管理员(root)",category = "4",admin_show=True)
async def handle_del_admin(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return
    if str(msg.user_id)!= str(admin_id):
        await bot.api.post_private_msg(msg.user_id, text="你没有权限删除管理员喵~")
        return

    if msg.raw_message.startswith("/del_admin"):
        id = msg.raw_message[len("/del_admin"):].strip()
    else:
        id = msg.raw_message[len("/da"):].strip()

    if id in admin:
        admin.remove(id)
        write_admin()
        await bot.api.post_private_msg(msg.user_id, text="删除成功喵~，现在"+id+"不是管理员喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="没有这个管理员喵~")

@register_command("/get_admin","/ga",help_text = "/get_admin 或者 /ga -> 获取管理员",category = "4")
async def handle_get_admin(msg, is_group=True):
    if is_group:
        await msg.reply(text="管理员列表："+str(admin))
    else:
        await bot.api.post_private_msg(msg.user_id, text="管理员列表："+str(admin))


@register_command("/set_ids",help_text = "/set_ids <昵称> <个性签名> <性别> -> 设置账号信息(管理员)",category = "4",admin_show=True)
async def handle_set(msg, is_group=True):
    """
            nickname: 昵称
            personal_note: 个性签名
            sex: 性别
            :return: 设置账号信息
    """
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return
    if str(msg.user_id) not in admin:
        await bot.api.post_private_msg(msg.user_id, text="你没有权限设置账号信息喵~")
        return
    msgs = msg.raw_message[len("/set_ids"):].split(" ")
    if len(msgs) < 3:
        text = "格式错误喵~ 请输入 /set 昵称 个性签名 性别"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    try:
        nickname = msgs[0]
        personal_note = msgs[1]
        sex = msgs[2]
        await bot.api.set_qq_profile(nickname=nickname, personal_note=personal_note, sex=sex)
        text = "设置成功喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
    except Exception:
        text = "设置失败喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/set_online_status",help_text = "/set_online_status <在线状态> -> 设置在线状态(管理员)",category = "4",admin_show=True)
async def handle_set_online_status(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return
    if str(msg.user_id) not in admin:
        await bot.api.post_private_msg(msg.user_id, text="你没有权限设置在线状态喵~")
        return
    msgs = msg.raw_message[len("/set_online_status"):].split(" ")[0]
    await bot.api.set_online_status(msgs)
    text = "设置成功喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/get_friends",help_text = "/get_friends -> 获取好友列表（管理员）",category = "4",admin_show=True)
async def handle_get_friends(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊获取喵~")
        return
    if str(msg.user_id) not in admin:
        await bot.api.post_private_msg(msg.user_id, text="你没有权限获取好友列表喵~")
    friends = await bot.api.get_friend_list(False)
    if is_group:
        await msg.reply(text=friends)
    else:
        await bot.api.post_private_msg(msg.user_id, text=friends)

@register_command("/set_qq_avatar",help_text = "/set_qq_avatar <地址> -> 更改头像（管理员）",category = "4",admin_show=True)
async def handle_set_qq_avatar(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return

    if str(msg.user_id) not in admin:
        await bot.api.post_private_msg(msg.user_id, text="你没有权限设置头像喵~")
        return

    msgs = msg.raw_message[len("/set_qq_avatar"):]
    await bot.api.set_qq_avatar(msgs)
    text = "设置成功喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/send_like",help_text = "/send_like <目标QQ号> <次数> -> 发送点赞(admin)",category = "4",admin_show=True)
async def handle_send_like(msg, is_group=True):
    if str(msg.user_id) not in admin:
        if is_group:
            await msg.reply(text="你没有权限发送点赞喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="你没有权限发送点赞喵~")
        return

    msgs = msg.raw_message[len("/send_like"):].split(" ")
    if len(msgs) < 2:
        text = "格式错误喵~ 请输入 /send_like 目标QQ号 次数"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)

    target_qq = msgs[0]
    times = msgs[1]
    await bot.api.send_like(target_qq, times)
    text = "发送成功喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/set_group_admin",help_text = "/set_group_admin <目标QQ号> -> 设置群管理员(admin)",category = "4",admin_show=True)
async def handle_set_group_admin(msg, is_group=True):
    if not is_group:
        await bot.api.post_private_msg(msg.user_id, text="只能在群聊中设置群管理员喵~")
        return

    if str(msg.user_id) not in admin:
        await msg.reply(text="你没有权限设置群管理员喵~")
        return

    msgs = msg.raw_message[len("/set_group_admin"):].split(" ")[0]
    await bot.api.set_group_admin(msg.group_id, msgs,True)
    await msg.reply(text="设置成功喵~")

@register_command("/del_group_admin",help_text = "/del_group_admin <目标QQ号> -> 取消群管理员(admin)",category = "4",admin_show=True)
async def handle_del_group_admin(msg, is_group=True):
    if not is_group:
        await bot.api.post_private_msg(msg.user_id, text="只能在群聊中取消群管理员喵~")
        return

    if str(msg.user_id) not in admin:
        await msg.reply(text="你没有权限设置群管理员喵~")
        return

    msgs = msg.raw_message[len("/del_group_admin"):].split(" ")[0]
    await bot.api.set_group_admin(msg.group_id, msgs,False)
    await msg.reply(text="取消成功喵~")

@register_command("/show_chat","/sc",help_text = "/show_chat 或 /sc -> 发送完整聊天记录(仅群admin)",category = "2")
async def handle_show_chat(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        await msg.reply(text="你没有权限发送聊天记录喵~")
        return
    # 使用标准化路径，避免Windows路径问题
    cache_dir = normalize_file_path(os.path.join(load_address(),"聊天记录.txt"))
    
    # 判断是否为Web端（没有group_id且有send_file方法）
    is_web = hasattr(msg, 'send_file') and not hasattr(msg, 'message_type')
    
    if is_group and not is_web:
        # QQ群聊
        with open("saved_message/group_messages.json","r",encoding="utf-8") as f:
            group_messages = json.load(f)
        try:
            text = str(group_messages[str(msg.group_id)])
        except KeyError:
            text = "该群没有聊天记录喵~"
        with open(cache_dir,"w",encoding="utf-8") as f:
            f.write(text)
        await bot.api.post_group_file(msg.group_id, file=cache_dir)

    else:
        # 私聊或Web端
        with open("saved_message/user_messages.json","r",encoding="utf-8") as f:
            user_messages = json.load(f)
        try:
            text = str(user_messages[str(msg.user_id)])
        except KeyError:
            text = "你没有聊天记录喵~"
        with open(cache_dir,"w",encoding="utf-8") as f:
            f.write(text)
        
        # Web端使用send_file方法
        if is_web and hasattr(msg, 'send_file'):
            await msg.send_file(cache_dir, "聊天记录.txt")
        else:
            # QQ私聊
            await bot.api.upload_private_file(msg.user_id, file=cache_dir,name="聊天记录.txt")

    os.remove(cache_dir)    

def _extract_history_text_item(item):
    raw = None
    if hasattr(item, "raw_message"):
        raw = getattr(item, "raw_message")
    if raw is None and isinstance(item, dict):
        raw = item.get("raw_message")
    if raw is None:
        msg_val = None
        if hasattr(item, "message"):
            msg_val = getattr(item, "message")
        elif isinstance(item, dict):
            msg_val = item.get("message")
        if msg_val is not None:
            raw = msg_val
    if raw is None:
        raw = ""
    return str(raw)

@register_command("/summary_recent","/sr", help_text="/summary_recent [数量] 或 /sr [数量] -> 总结最近若干条群聊消息", category="2")
async def handle_summary_recent(msg, is_group=True):
    if not is_group:
        await bot.api.post_private_msg(msg.user_id, text="请在群聊中使用该命令喵~")
        return
    parts = msg.raw_message.split()
    count = 200
    if len(parts) >= 2:
        try:
            count = int(parts[1])
        except ValueError:
            await msg.reply(text="格式错误喵~ 请输入 /summary_recent [数量] 或 /sr [数量]")
            return
    if count <= 0:
        count = 1
    if count > 500:
        count = 500
    try:
        history = await bot.api.get_group_msg_history(msg.group_id, message_seq=0, count=count, reverse_order=True)
    except Exception as e:
        await msg.reply(text=f"获取群聊历史失败喵~：{e}")
        return
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
    if not items:
        await msg.reply(text="没有获取到群聊历史消息喵~")
        return
    lines = []
    for item in items:
        user_id = None
        nickname = ""
        if isinstance(item, dict):
            user_id = item.get("user_id")
            sender = item.get("sender")
            if isinstance(sender, dict):
                nickname = sender.get("nickname", "") or ""
        else:
            if hasattr(item, "user_id"):
                user_id = getattr(item, "user_id")
            sender = getattr(item, "sender", None)
            if sender is not None:
                try:
                    nickname = sender.nickname
                except Exception:
                    if isinstance(sender, dict):
                        nickname = sender.get("nickname", "") or ""
        text = _extract_history_text_item(item)
        uid_str = str(user_id) if user_id is not None else ""
        name_part = nickname or uid_str
        if not name_part:
            line = text
        else:
            line = f"{name_part}: {text}"
        lines.append(line)
    log_text = "\n".join(lines)
    summary = summarize_group_text(log_text)
    await msg.reply(text=summary)

async def get_group_today_summary_text(group_id):
    max_count = 500
    try:
        history = await bot.api.get_group_msg_history(
            group_id,
            message_seq=0,
            count=max_count,
            reverse_order=True,
        )
    except Exception as e:
        _log.error(f"获取群聊历史失败喵~：{e}")
        return None
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
    if not items:
        return "今天群里还没有记录到消息喵~"
    today = datetime.now().date()
    filtered = []
    for item in items:
        t = None
        if isinstance(item, dict):
            t = item.get("time")
        else:
            if hasattr(item, "time"):
                t = getattr(item, "time")
        try:
            dt = datetime.fromtimestamp(int(t)) if t is not None else None
        except Exception:
            dt = None
        if dt is not None and dt.date() == today:
            filtered.append(item)
    if not filtered:
        return "今天群里还没有记录到消息喵~"
    lines = []
    for item in filtered:
        user_id = None
        nickname = ""
        if isinstance(item, dict):
            user_id = item.get("user_id")
            sender = item.get("sender")
            if isinstance(sender, dict):
                nickname = sender.get("nickname", "") or ""
        else:
            if hasattr(item, "user_id"):
                user_id = getattr(item, "user_id")
            sender = getattr(item, "sender", None)
            if sender is not None:
                try:
                    nickname = sender.nickname
                except Exception:
                    if isinstance(sender, dict):
                        nickname = sender.get("nickname", "") or ""
        text = _extract_history_text_item(item)
        uid_str = str(user_id) if user_id is not None else ""
        name_part = nickname or uid_str
        if not name_part:
            line = text
        else:
            line = f"{name_part}: {text}"
        lines.append(line)
    log_text = "\n".join(lines)
    return summarize_group_text(log_text)

@register_command("/summary_today", help_text="/summary_today -> 总结今天与机器人的聊天内容", category="2")
async def handle_summary_today(msg, is_group=True):
    if is_group:
        summary = await get_group_today_summary_text(msg.group_id)
        if summary:
            await msg.reply(text=summary)
    else:
        user_id = msg.user_id
        summary = generate_today_summary(user_id=user_id)
        await bot.api.post_private_msg(user_id, text=summary)

async def auto_summary_task():
    """每日自动总结定时任务"""
    _log.info("每日自动总结定时任务已启动")
    while True:
        try:
            now = datetime.now()
            # 每天 23:55 执行
            if now.hour == 23 and now.minute == 55:
                _log.info("开始执行每日自动总结任务")
                # 遍历所有群组开关
                for group_id, switches in switch.group_switches.items():
                    if switches.get('summary_auto', False):
                        try:
                            summary = await get_group_today_summary_text(int(group_id))
                            if summary and "今天群里还没有记录到消息喵" not in summary:
                                await bot.api.post_group_msg(group_id=int(group_id), text=f"【每日自动总结】\n{summary}")
                                _log.info(f"已向群 {group_id} 发送自动总结")
                        except Exception as e:
                            _log.error(f"自动总结群 {group_id} 失败: {e}")
                # 执行完后等 65 秒，确保不会在同一分钟内再次触发
                await asyncio.sleep(65)
            else:
                # 每 30 秒检查一次时间
                await asyncio.sleep(30)
        except Exception as e:
            _log.error(f"每日自动总结任务发生异常: {e}")
            await asyncio.sleep(60)


async def auto_active_chat_task():
    _log.info("主动聊天定时任务已启动")
    while True:
        try:
            now = datetime.now()
            _log.debug(f"主动聊天定时任务检查中... 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
            if 8 <= now.hour < 24:
                current_time = time.time()
                _log.debug(f"[主动聊天] 正在检查 {len(running)} 个用户的主动聊天状态")
                for user_id, info in list(running.items()):
                    if not info.get("active", False):
                        continue
                    interval = float(info.get("interval", 1.0))
                    last_time = normalize_timestamp(info.get("last_time", 0))
                    _log.debug(f"[主动聊天] 用户 {user_id} - interval: {interval}h, last_time: {last_time}, current_time: {current_time}")
                    if last_time == 0:
                        running[user_id]["last_time"] = current_time
                        write_running()
                        _log.debug(f"[主动聊天] 用户 {user_id} 初始化 last_time")
                        continue
                    time_diff_hours = (current_time - last_time) / 3600
                    _log.debug(f"[主动聊天] 用户 {user_id} 距离上次互动: {time_diff_hours:.2f} 小时")
                    if current_time - last_time >= 60 * 60 * interval:
                        _log.debug(f"[主动聊天] 用户 {user_id} 达到触发条件，准备调用 heartbeat")
                        try:
                            next_interval = await heartbeat_core.process_user(int(user_id), interval)
                            if next_interval is not None:
                                running[user_id]["interval"] = next_interval
                            running[user_id]["last_time"] = current_time
                            write_running()
                        except Exception as e:
                            _log.error(f"主动聊天用户 {user_id} 发送失败: {e}")
                    else:
                        _log.debug(f"[主动聊天] 用户 {user_id} 未达到触发条件 (需要 {60 * 60 * interval}秒，已过 {current_time - last_time}秒)")
            else:
                _log.debug(f"[主动聊天] 当前时间 {now.hour} 点，不在 8-24 点主动聊天时段，跳过")
            await asyncio.sleep(60)
        except Exception as e:
            _log.error(f"主动聊天定时任务发生异常: {e}")
            await asyncio.sleep(60)

@register_command("/summary_auto", help_text="/summary_auto -> 开启或关闭每日自动总结群聊记录(admin)", category="2", admin_show=True)
async def handle_summary_auto(msg, is_group=True):
    if not is_group:
        await bot.api.post_private_msg(msg.user_id, text="请在群聊中使用该命令喵~")
        return
    if str(msg.user_id) not in admin:
        await msg.reply(text="你没有权限开启自动总结喵~")
        return
    
    state = switch.toggle_switch('summary_auto', group_id=str(msg.group_id))
    text = "已开启每日自动总结喵~（将在每天23:55发送）" if state else "已关闭每日自动总结喵~"
    await msg.reply(text=text)
    switch.save_switches()

@register_command("/auto_reply", help_text="/auto_reply [话痨程度0-1] -> 开启或关闭群聊智能自动回复(admin)", category="2", admin_show=True)
async def handle_auto_reply(msg, is_group=True):
    if not is_group:
        await bot.api.post_private_msg(msg.user_id, text="请在群聊中使用该命令喵~")
        return
    if str(msg.user_id) not in admin:
        await msg.reply(text="你没有权限开启智能自动回复喵~")
        return
    group_id_str = str(msg.group_id)
    raw = msg.raw_message[len("/auto_reply"):].strip()
    level = None
    if raw:
        try:
            level = float(raw.split()[0])
        except ValueError:
            await msg.reply(text="格式错误喵~ 请输入 0~1 之间的小数，例如 0.3 或 0.8")
            return
        if level < 0:
            level = 0.0
        if level > 1:
            level = 1.0
        if group_id_str not in switch.group_switches:
            switch.group_switches[group_id_str] = {}
        switch.group_switches[group_id_str]['auto_reply_level'] = level
        switch.save_switches()
    state = switch.toggle_switch('auto_reply', group_id=group_id_str)
    current_level = level
    if current_level is None:
        try:
            current_level = float(switch.group_switches.get(group_id_str, {}).get('auto_reply_level', 0.5))
        except Exception:
            current_level = 0.5
    text = ("已开启群聊智能自动回复喵~" if state else "已关闭群聊智能自动回复喵~") + f" 当前话痨程度：{current_level:.2f}"
    await msg.reply(text=text)

@register_command("/主动聊天",help_text = "/主动聊天 [是否开启(1/0)] -> 开启/关闭主动聊天（AI将自行决定聊天频率）",category = "2")
async def handle_active_chat(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return

    try:
        raw = msg.raw_message[len("/主动聊天"):].strip()
        parts = raw.split() if raw else []
        user_id = str(msg.user_id)
        current = running.get(user_id, {})
        
        # 默认值
        interval = float(current.get("interval", 1.0)) # 默认初始间隔1小时
        active = bool(current.get("active", False))

        if not parts:
            active = not active
        elif len(parts) == 1:
            if parts[0] in ("0", "1"):
                active = bool(int(parts[0]))
            else:
                 # 兼容旧指令，如果是数字但不是0/1，认为是设置初始间隔（虽然现在AI会动态调整）
                try:
                    interval = float(parts[0])
                    active = True
                except ValueError:
                    raise ValueError
        elif len(parts) >= 2:
             # 兼容旧指令：间隔 + 开关
             interval = float(parts[0])
             if parts[1] in ("0", "1"):
                 active = bool(int(parts[1]))
        
        if user_id not in running:
            running[user_id] = {}
        
        running[user_id]["interval"] = interval
        running[user_id]["active"] = active
        running[user_id]["state"] = False
        switch.set_switch_state('active_chat', active, user_id=user_id)
        
        # 如果开启，尝试获取最近消息时间作为基准
        if active:
            try:
                ori = await bot.api.get_recent_contact(100)
                for contact in ori.get("data", []):
                    if str(contact['lastestMsg'].get("user_id")) == user_id:
                        running[user_id]["last_time"] = normalize_timestamp(contact['lastestMsg'].get("time", 0))
                        break
                # 如果没找到最近联系记录，就用当前时间
                if "last_time" not in running[user_id]:
                    running[user_id]["last_time"] = time.time()
            except Exception as e:
                _log.error(f"获取最近联系人失败: {e}")
                running[user_id]["last_time"] = time.time()
                
        write_running()
        reply = f"设置成功喵~，{'AI现在会自行决定什么时候找你聊天喵~' if active else '已关闭主动聊天喵~'}"
        await bot.api.post_private_msg(user_id, text=reply)
    except ValueError:
        await bot.api.post_private_msg(msg.user_id, text="格式错误喵~ 请输入 /主动聊天 [1/0]")

# 添加临时存储字典
temp_selections = {}
api_book = {}
WENKU8_COOKIE = "" # 去 www.wenku8.net 登录获取
def load_wenku8_cookie():
    global WENKU8_COOKIE
    if os.path.exists("wenku8_cookie.txt"):
        with open("wenku8_cookie.txt", "r", encoding="utf-8") as f:
            WENKU8_COOKIE = f.read().strip()

def save_wenku8_cookie(cookie):
    global WENKU8_COOKIE
    WENKU8_COOKIE = cookie
    with open("wenku8_cookie.txt", "w", encoding="utf-8") as f:
        f.write(cookie)

def check_wenku8_cookie() -> str:
    """
    检查 WENKU8_COOKIE 是否已设置
    :return: 如果未设置返回提示消息，否则返回 None
    """
    if not WENKU8_COOKIE:
        return "❌ Cookie 未设置喵！\n请管理员使用 `/set_wenku_cookie <Cookie>` 命令设置 Cookie 喵~\n或者去 www.wenku8.net 登录后获取 Cookie"
    return None

load_wenku8_cookie()

NOVEL_API_BASE_URLS = [
    "http://43.248.77.205:22222",
    "https://fq.shusan.cn",
]
_novel_api_base_url = None

def get_novel_api_base_url():
    global _novel_api_base_url
    if _novel_api_base_url:
        return _novel_api_base_url

    for base_url in NOVEL_API_BASE_URLS:
        try:
            url = f"{base_url.rstrip('/')}/api/search"
            res = requests.get(url, params={"key": "test", "tab_type": 3}, timeout=5)
            if not res.ok:
                continue
            data = res.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                _novel_api_base_url = base_url.rstrip("/")
                return _novel_api_base_url
        except Exception:
            continue

    return None

@register_command("/findbook","/fb",help_text="/findbook 或者 /fb <书名> -> 搜索并选择下载轻小说",category = "6")
async def handle_find_book(msg, is_group=True):
    # 检查 Cookie
    cookie_check = check_wenku8_cookie()
    if cookie_check:
        if is_group:
            await msg.reply(text=cookie_check)
        else:
            await bot.api.post_private_msg(msg.user_id, text=cookie_check)
        return
    
    search_term = ""
    if msg.raw_message.startswith("/findbook"):
        search_term = msg.raw_message[len("/findbook"):].strip()
    elif msg.raw_message.startswith("/fb"):
        search_term = msg.raw_message[len("/fb"):].strip()
    if not search_term:
        reply = "请输入要搜索的书名喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    # 发送通知
    if is_group:
        await msg.reply(text="正在搜索轻小说喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索轻小说喵~")

    # 创建HTML文件
    cache_dir = os.path.join(load_address(), "novel_search")
    os.makedirs(cache_dir, exist_ok=True)
    file_token = hashlib.md5(f"novel_{search_term}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{search_term}.html"
    filepath = os.path.join(cache_dir, filename)

    build_novel_grid_html(f"{html.escape(search_term)} · 轻小说搜索", filepath)

    # 1. 先搜索本地缓存
    local_matches = search_local_novel_cache(search_term)
    
    # 2. 如果本地结果不够（少于5本），再去网站搜索
    web_matches = []
    if len(local_matches) < 5:
        web_matches = search_wenku8_books(search_term, "articlename")
    
    # 3. 合并结果并去重（以书名作为唯一标识）
    seen_titles = set()
    matches = []
    
    # 先添加本地结果
    for author, title, download_url in local_matches:
        if title not in seen_titles:
            seen_titles.add(title)
            matches.append((author, title, download_url))
    
    # 再添加网站搜索结果（去重）
    for author, title, download_url in web_matches:
        if title not in seen_titles:
            seen_titles.add(title)
            matches.append((author, title, download_url))
    
    # 4. 搜索API书籍
    api_book[msg.user_id] = find_book_from_api(search_term)

    if not matches and not api_book[msg.user_id]:
        close_novel_grid_html(filepath)
        reply = f"没有找到包含'{search_term}'的轻小说喵~"
        if is_group:
            await msg.reply(text=reply)
            await bot.api.post_group_file(msg.group_id, file=filepath)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
            await bot.api.upload_private_file(msg.user_id, filepath, filename)
        return

    # 添加搜索结果到卡片
    for i, (author, title, download_url) in enumerate(matches):
        book_id_match = re.search(r'id=(\d+)', download_url)
        book_id = book_id_match.group(1) if book_id_match else "0"
        append_novel_card(filepath, book_id, title, author, i + 1)
        # 更新全局 books 字典
        if title not in books:
            books[title] = {
                "download_url": download_url,
                "page": f"https://www.wenku8.net/book/{book_id}.htm",
                "author": author
            }

    # 添加 API 结果到卡片
    if api_book[msg.user_id]:
        start_seq = len(matches) + 1
        for i, (_, title) in enumerate(api_book[msg.user_id].items()):
            if title not in seen_titles:  # API结果也去重
                book_id = api_book[msg.user_id][_].get("book_id", "0")
                author = api_book[msg.user_id][_].get("author", "未知")
                append_novel_card(filepath, book_id, title, author, start_seq + i)
                # 更新全局 books 字典
                if title not in books:
                    books[title] = api_book[msg.user_id][_]

    close_novel_grid_html(filepath)

    # 存储匹配结果临时数据
    temp_selections[msg.user_id] = matches

    if is_group:
        await msg.reply(text=f"找到 {len(matches) + len(api_book[msg.user_id]) if api_book[msg.user_id] else len(matches)} 本轻小说喵~ 点击卡片查看详情或使用 /info 编号 获取信息喵~")
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.post_private_msg(msg.user_id, text=f"找到 {len(matches) + len(api_book[msg.user_id]) if api_book[msg.user_id] else len(matches)} 本轻小说喵~ 点击卡片查看详情或使用 /info 编号 获取信息喵~")
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/fa",help_text="/fa <作者> -> 搜索作者",category = "6")
async def handle_find_author(msg, is_group=True):
    # 检查 Cookie
    cookie_check = check_wenku8_cookie()
    if cookie_check:
        if is_group:
            await msg.reply(text=cookie_check)
        else:
            await bot.api.post_private_msg(msg.user_id, text=cookie_check)
        return
    
    search_term = msg.raw_message[len("/fa"):].strip()
    if not search_term:
        reply = "请输入要搜索的作者喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    matches = search_wenku8_books(search_term, "author")

    if not matches:
        reply = f"没有找到包含'{search_term}'的作者喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    # 生成选择列表
    choices = "\n".join([f"{i+1}. {title} -- {author}" for i, (author,title, _) in enumerate(matches)])
    reply = f"找到以下匹配的作者的轻小说喵~:\n{choices}\n\n请回复'/select 编号'选择要下载的轻小说喵~\n回复'/info 编号'获取轻小说信息喵~"
    # 存储匹配结果临时数据
    temp_selections[msg.user_id] = matches

    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

def search_wenku8_books(search_term: str, search_type: str, max_pages: int = 3) -> list:
    """
    搜索 wenku8 小说，支持分页
    
    :param search_term: 搜索关键词
    :param search_type: 搜索类型 (articlename 或 author)
    :param max_pages: 最大搜索页数，默认为 3 页
    :return: 搜索结果列表
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.wenku8.net/",
        "Cookie": WENKU8_COOKIE
    }
    _log.info(f"使用Cookie: {WENKU8_COOKIE[:50]}...")
    import urllib.parse
    try:
        encoded_key = urllib.parse.quote(search_term.encode("gbk"))
    except Exception as e:
        _log.error(f"搜索关键词编码失败: {search_term}, 错误: {e}")
        return []
    
    results = []
    pattern = r'<div style="width:373px;height:136px;float:left;margin:5px 0px 5px 5px;">(.*?)</div>\s*</div>'
    
    for page in range(1, max_pages + 1):
        url = f"https://www.wenku8.net/modules/article/search.php?searchtype={search_type}&searchkey={encoded_key}&page={page}"
        _log.info(f"搜索URL (第{page}页): {url}")
        try:
            response = requests.get(url, headers=headers, timeout=10)
            _log.info(f"搜索响应状态码: {response.status_code}")
        except Exception as e:
            _log.error(f"搜索请求失败: {e}")
            break
        
        # 检查网站是否关闭
        if '有缘再相聚' in response.text or '网站已关闭' in response.text:
            _log.warning("wenku8.net 网站已关闭")
            break
        
        response.encoding = "gbk"
        content = response.text
        
        # 检查HTTP状态码
        if response.status_code == 403:
            _log.warning("搜索返回403错误，Cookie可能已失效或被反爬虫机制拦截")
            break
        # 检查是否需要登录（根据特定错误提示，排除已登录状态下的"退出登录"字样）
        if "出现错误" in content or ("登录" in content and "退出登录" not in content):
            _log.warning("搜索需要登录，Cookie可能已失效")
            break
        
        page_matches = re.findall(pattern, content, re.DOTALL)
        _log.info(f"第{page}页搜索结果数量: {len(page_matches)}")
        
        if not page_matches:
            _log.info(f"第{page}页没有结果，停止搜索")
            break
        
        for match in page_matches:
            title_url_match = re.search(r'<b><a style="font-size:13px;" href="([^"]+)" title="([^"]+)" target="_blank">', match)
            book_url = title_url_match.group(1) if title_url_match else ""
            title = title_url_match.group(2) if title_url_match else "未知"
            book_id = "0"
            id_match = re.search(r'/book/(\d+)\.htm', book_url)
            if id_match:
                book_id = id_match.group(1)
            node = int(book_id) // 1000 if book_id.isdigit() else 0
            author_cat_match = re.search(r'<p>作者:([^/]+)/分类:([^<]+)</p>', match)
            author = author_cat_match.group(1) if author_cat_match else "未知"
            category = author_cat_match.group(2) if author_cat_match else "未知"
            stats_match = re.search(r'<p>更新:([^/]+)/字数:([^/]+)/([^<]+)</p>', match)
            last_date = stats_match.group(1) if stats_match else "未知"
            word_count = stats_match.group(2) if stats_match else "未知"
            is_serialize = stats_match.group(3) if stats_match else "未知"
            tags_match = re.search(r'Tags:<span[^>]*>([^<]+)</span>', match)
            tags = tags_match.group(1) if tags_match else "无"
            intro_match = re.search(r'简介:([^<]+)', match)
            introduction = intro_match.group(1).strip() if intro_match else "暂无简介"
            img_match = re.search(r'<img src="([^"]+)"', match)
            cover_url = img_match.group(1) if img_match else f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg"
            download_url = f"https://dl.wenku8.com/down.php?type=txt&node={node}&id={book_id}"
            page_url = f"https://www.wenku8.net/book/{book_id}.htm"
            books[title] = {
                "author": author,
                "category": category,
                "last_date": last_date,
                "word_count": word_count,
                "is_serialize": is_serialize,
                "introduction": introduction,
                "tags": tags,
                "cover_url": cover_url,
                "download_url": download_url,
                "page": page_url,
                "hot": "搜索结果书籍"
            }
            results.append((author, title, download_url))
    
    _log.info(f"总搜索结果数量: {len(results)}")
    return results

def search_local_novel_cache(search_term: str) -> list:
    """
    在本地 novel_details2.json 缓存中搜索小说
    
    :param search_term: 搜索关键词
    :return: 搜索结果列表 [(author, title, download_url), ...]
    """
    results = []
    search_term_lower = search_term.lower()
    
    try:
        cache_file = os.path.join(os.path.dirname(__file__), '..', 'resources', 'config', 'novel_details2.json')
        if not os.path.exists(cache_file):
            _log.warning(f"本地缓存文件不存在: {cache_file}")
            return results
        
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        for title, info in cache_data.items():
            # 在书名和作者中搜索
            author = info.get('author', '')
            if search_term_lower in title.lower() or search_term_lower in author.lower():
                download_url = info.get('download_url', '')
                results.append((author, title, download_url))
        
        _log.info(f"本地缓存搜索结果: 找到 {len(results)} 本包含 '{search_term}' 的小说")
    except Exception as e:
        _log.error(f"搜索本地缓存失败: {e}")
    
    return results

def get_book_detail_by_url(book_url: str) -> dict:
    """
    通过URL获取小说详情（类似/hotnovel的实现）
    :param book_url: 小说详情页面URL，如 https://www.wenku8.net/book/1234.htm 或 /book/1234.htm
    :return: 包含小说详细信息的字典
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.wenku8.net/",
        "Cookie": WENKU8_COOKIE
    }

    # 处理相对路径
    if not book_url.startswith(('http://', 'https://')):
        book_url = 'https://www.wenku8.net' + book_url

    _log.info(f"获取小说详情: {book_url}")
    try:
        response = requests.get(book_url, headers=headers, timeout=10)
        _log.info(f"详情页响应状态码: {response.status_code}")
        
        # 检查网站是否关闭
        if response.status_code == 404 or '网站已关闭' in response.text or '有缘再相聚' in response.text:
            _log.warning("wenku8.net 网站已关闭或页面不存在")
            return None
        
        response.encoding = "gbk"
        content = response.text
        
        # 解析书籍ID
        book_id_match = re.search(r'/book/(\d+)\.htm', book_url)
        book_id = book_id_match.group(1) if book_id_match else "0"
        node = int(book_id) // 1000 if book_id.isdigit() else 0
        
        # 解析标题
        title_match = re.search(r'<span property="v:itemreviewed">([^<]+)</span>', content)
        title = title_match.group(1).strip() if title_match else "未知"
        
        # 解析作者
        author_match = re.search(r'作者：\s*<a[^>]*>([^<]+)</a>', content)
        author = author_match.group(1).strip() if author_match else "未知"
        
        # 解析分类
        category_match = re.search(r'类别：\s*<a[^>]*>([^<]+)</a>', content)
        category = category_match.group(1).strip() if category_match else "未知"
        
        # 解析状态
        status_match = re.search(r'状态：\s*<font[^>]*>([^<]+)</font>', content)
        is_serialize = status_match.group(1).strip() if status_match else "未知"
        
        # 解析字数
        word_count_match = re.search(r'字数：\s*([\d,]+)', content)
        word_count = word_count_match.group(1).replace(',', '') if word_count_match else "未知"
        
        # 解析更新时间
        update_match = re.search(r'更新时间：\s*([\d-]+)', content)
        last_date = update_match.group(1) if update_match else "未知"
        
        # 解析简介
        intro_match = re.search(r'<span class="hottext">内容简介：</span>\s*<br\s*/?>\s*([^<]+)', content, re.DOTALL)
        introduction = intro_match.group(1).strip() if intro_match else "暂无简介"
        
        # 解析封面
        cover_match = re.search(r'<img src="([^"]+)"[^>]*alt="[^"]*封面"', content)
        cover_url = cover_match.group(1) if cover_match else f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg"
        
        # 构建下载链接
        download_url = f"https://dl.wenku8.com/down.php?type=txt&node={node}&id={book_id}"
        
        return {
            "title": title,
            "author": author,
            "category": category,
            "word_count": word_count,
            "is_serialize": is_serialize,
            "last_date": last_date,
            "introduction": introduction,
            "cover_url": cover_url,
            "download_url": download_url,
            "page": book_url,
            "hot": "URL获取"
        }
    except Exception as e:
        _log.error(f"获取小说详情失败: {book_url}, 错误: {e}")
        return None

def find_book_from_api(search_term: str) -> list:
    """
    从API搜索小说
    :param search_term: 搜索关键词
    :return: 包含匹配小说信息的列表
    """
    base_url = get_novel_api_base_url()
    if not base_url:
        return {}
    url = f"{base_url}/api/search"
    params = {
        "key": search_term,
        "tab_type": 3,
    }
    try:
        res = requests.get(url, params=params, timeout=10)
    except Exception:
        return {}
    book_ids = {}
    if res.ok:
        data = res.json()
        for tab in data.get("data", {}).get("search_tabs", []):
            if tab.get("tab_type") == 3:  # 书籍类 tab
                for item in tab.get("data", []):
                    book_data = item.get("book_data", [])
                    for book in book_data:
                        book_id = book.get("book_id")
                        book_name = book.get("book_name")
                        if book_id:
                            book_ids[book_id] = book_name
    
    return book_ids


def download_api_book(id,name):
    base_url = get_novel_api_base_url()
    if not base_url:
        _log.error("下载失败：没有可用的API地址")
        return
    url = f"{base_url}/api/content"
    params = {
        "tab":"下载",
        "book_id":id
    }
    try:
        response = requests.get(url, params=params, timeout=30)
    except Exception as e:
        _log.error(f"下载失败：{e}")
        return
    if response.status_code == 200:
        content = response.text
        # 确保目录存在
        path = os.path.join(os.path.dirname(__file__), f"cache/novel/{name}.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
    else:
        _log.error(f"下载失败，状态码：{response.status_code}")

def get_api_book_info(id):
    base_url = get_novel_api_base_url()
    if not base_url:
        _log.error("获取失败：没有可用的API地址")
        return None
    url = f"{base_url}/api/detail"
    params = {
        "book_id":id
    }
    try:
        response = requests.get(url, params=params, timeout=10)
    except Exception as e:
        _log.error(f"获取失败：{e}")
        return None
    if response.status_code == 200:
        raw = json.loads(response.text)
        book  = raw['data']['data']          

        info = {
            'author'       : book['author'],
            'category'     : book['category'],
            'word_count'   : f"{int(book['word_number']):,}",  
            'is_serialize' : '连载中' if int(book['creation_status']) == 1 else '已完结',
            'hot'          : book['read_cnt_text'],            
            'last_date'    : datetime.fromtimestamp(int(book['last_publish_time'])).strftime('%Y-%m-%d'),
            'download_url' : f"https://tomato-novel-downloader.vercel.app/?book_id={book['book_id']}", 
            'page'         : f"https://fanqienovel.com/page/{book['book_id']}",                       
            'cover'        : book['thumb_url'],
            'introduction' : book['abstract'].replace('\n', ''),   
            'title'        : book['book_name'],
        }
        return info

    else:
        _log.error(f"获取失败，状态码：{response.status_code}")
        return None


# 添加选择处理函数
@register_command("/select", help_text="/select <编号> -> 选择要下载的轻小说(先使用/findbook或/fb搜索，再进行选择，重复使用/fb会覆盖之前的搜索结果)",category = "6")
async def handle_select_book(msg, is_group=True):
    if (msg.user_id not in temp_selections) and (msg.user_id not in api_book):
        reply = "没有找到主人的搜索记录喵~请先使用/findbook搜索喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return
    try:
        selection = int(msg.raw_message[len("/select"):].strip()) - 1
        matches = temp_selections.get(msg.user_id, [])
        api_books = api_book.get(msg.user_id, {})

        if 0 <= selection < len(matches) or (selection >= len(matches) and selection < len(matches) + len(api_books)):
            if selection < len(matches):
                author,title, url = matches[selection]
                reply = f"已选择《{title}》-- {author}喵~\n下载链接: {url}"
                if is_group:
                    await msg.reply(text=reply)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=reply)
                # 注意：这里只发送链接，不直接发送文件，因为URL不是本地文件
                # 如果需要下载文件，需要先下载到本地再发送
            else:
                id, title = list(api_books.items())[selection - len(matches)]
                download_api_book(id,title)
                reply = f"已开始下载《{title}》喵~"
                await msg.reply(text=reply)
                api_book_file_path = os.path.join(os.path.dirname(__file__), f"cache/novel/{title}.txt")
                if os.path.exists(api_book_file_path):
                    if is_group:
                        await bot.api.post_group_file(msg.group_id,file=api_book_file_path)
                    else:
                        await bot.api.upload_private_file(msg.user_id,file=api_book_file_path,name=title+".txt")
                else:
                    await msg.reply(text="文件下载中，请稍后再试喵~")
            
        else:
            reply = "编号无效喵~请选择列表中的编号喵~"
            if is_group:
                await msg.reply(text=reply)
            else:
                await bot.api.post_private_msg(msg.user_id, text=reply)
    except ValueError:
        reply = "请输入有效的编号喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
    
    # 安全删除，避免KeyError
    temp_selections.pop(str(msg.user_id), None)
    api_book.pop(str(msg.user_id), None)  

@register_command("/info",help_text="/info <书名> -> 获取轻小说信息",category = "6")
async def handle_info(msg, is_group=True):
    # 检查 Cookie
    cookie_check = check_wenku8_cookie()
    if cookie_check:
        if is_group:
            await msg.reply(text=cookie_check)
        else:
            await bot.api.post_private_msg(msg.user_id, text=cookie_check)
        return
    
    if (msg.user_id not in temp_selections) and (msg.user_id not in api_book):
        reply = "没有找到您的搜索记录喵~请先使用/findbook搜索喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return 
    try:
        selection = int(msg.raw_message[len("/info"):].strip()) - 1
        matches = temp_selections[msg.user_id]
        api_books = api_book[msg.user_id]

        if 0 <= selection < len(matches) or (selection >= len(matches) and selection < len(matches) + len(api_books)):
            if selection < len(matches):
                author,title, url = matches[selection]
                # 从下载链接提取book_id，构建详情页面URL
                book_id_match = re.search(r'id=(\d+)', url)
                if book_id_match:
                    book_id = book_id_match.group(1)
                    book_url = f"https://www.wenku8.net/book/{book_id}.htm"
                    info = get_book_detail_by_url(book_url)
                else:
                    info = None
                
                if info is None:
                    # 如果URL获取失败，先尝试从JSON/内存字典获取
                    info = books.get(title)
                    if info is None:
                        # 如果JSON也没有，使用搜索时的基本信息
                        info = {
                            'author': author,
                            'category': '未知',
                            'word_count': '未知',
                            'is_serialize': '未知',
                            'hot': '搜索结果',
                            'introduction': '暂无简介',
                            'last_date': '未知',
                            'page': book_url if 'book_url' in locals() else url,
                            'cover_url': f"https://img.wenku8.com/image/{int(book_id)//1000 if 'book_id' in locals() else 0}/{book_id if 'book_id' in locals() else 0}/{book_id if 'book_id' in locals() else 0}s.jpg"
                        }
                    else:
                        _log.info(f"从JSON/内存字典获取到《{title}》的信息")
                
                try:
                    introduction = info['introduction']
                except Exception:
                    introduction = "暂无"
                cover = info['cover_url']
                
                # 创建HTML详情页
                cache_dir = os.path.join(load_address(), "novel_info")
                os.makedirs(cache_dir, exist_ok=True)
                file_token = hashlib.md5(f"info_{title}_{time.time()}".encode("utf-8")).hexdigest()[:8]
                filename = f"{file_token}_{title[:20]}.html"
                filepath = os.path.join(cache_dir, filename)
                
                build_novel_detail_html(title, info, filepath)
                
                # 发送文本消息
                text_reply = f"《{title}》的信息如下喵~\n作者: {author}\n分类: {info['category']}\n字数: {info['word_count']}\n状态: {info['is_serialize']}\n热度：{info['hot']}\n简介：{introduction}\n更新日期: {info['last_date']}\n下载链接: {url}\n详细页面：{info['page']}"
                
                if is_group:
                    await msg.reply(text=text_reply)
                    await bot.api.post_group_file(msg.group_id, file=filepath)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=text_reply)
                    await bot.api.upload_private_file(msg.user_id, filepath, filename)
            else:
                id, title = list(api_books.items())[selection - len(matches)]
                
                info = get_api_book_info(id)
                if(info==None):
                    reply = "没有找到该轻小说的信息喵~"
                    await msg.reply(text=reply)
                    return

                # 创建HTML详情页
                cache_dir = os.path.join(load_address(), "novel_info")
                os.makedirs(cache_dir, exist_ok=True)
                file_token = hashlib.md5(f"info_{title}_{time.time()}".encode("utf-8")).hexdigest()[:8]
                filename = f"{file_token}_{title[:20]}.html"
                filepath = os.path.join(cache_dir, filename)
                
                build_novel_detail_html(title, info, filepath)
                
                # 发送文本消息
                text_reply = f"《{title}》的信息如下喵~\n作者: {info['author']}\n分类: {info['category']}\n字数: {info['word_count']}\n状态: {info['is_serialize']}\n热度：{info['hot']}\n简介：{info['introduction']}\n更新日期: {info['last_date']}\n下载链接: {info['download_url']}\n详细页面：{info['page']}"
                
                if is_group:
                    await msg.reply(text=text_reply)
                    await bot.api.post_group_file(msg.group_id, file=filepath)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=text_reply)
                    await bot.api.upload_private_file(msg.user_id, filepath, filename)

        else:
            reply = "编号无效喵~请选择列表中的编号喵~"
            if is_group:
                await msg.reply(text=reply)
            else:
                await bot.api.post_private_msg(msg.user_id, text=reply)
    except ValueError:
        reply = "请输入有效的编号喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

def get_random_book_from_hotlist() -> tuple:
    """
    从今日热门榜单获取随机小说
    :return: (title, book_info_dict) 或 (None, None)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.wenku8.net/",
        "Cookie": WENKU8_COOKIE
    }
    url = "https://www.wenku8.net/modules/article/toplist.php?sort=dayvisit&page=1"
    
    _log.info(f"获取热门榜单: {url}")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        _log.info(f"热门榜单响应状态码: {response.status_code}")
        response.encoding = 'gbk'
        content = response.text
        
        pattern = r'<div style="width:373px;height:136px;float:left;margin:5px 0px 5px 5px;">(.*?)</div>\s*</div>'
        page_matches = re.findall(pattern, content, re.DOTALL)
        
        if not page_matches:
            return None, None
        
        # 随机选择一本
        match = random.choice(page_matches)
        
        # 解析标题和URL
        title_url_match = re.search(r'<b><a style="font-size:13px;" href="([^"]+)" title="([^"]+)" target="_blank">', match)
        if not title_url_match:
            return None, None
        
        book_url = title_url_match.group(1)
        title = title_url_match.group(2)
        
        # 获取详细信息
        info = get_book_detail_by_url(book_url)
        if info is None:
            # 如果详情获取失败，使用基本信息
            book_id_match = re.search(r'/book/(\d+)\.htm', book_url)
            book_id = book_id_match.group(1) if book_id_match else "0"
            node = int(book_id) // 1000 if book_id.isdigit() else 0
            
            author_cat_match = re.search(r'<p>作者:([^/]+)/分类:([^<]+)</p>', match)
            author = author_cat_match.group(1) if author_cat_match else "未知"
            
            info = {
                'author': author,
                'category': '未知',
                'word_count': '未知',
                'is_serialize': '未知',
                'last_date': '未知',
                'introduction': '暂无简介',
                'cover_url': f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg",
                'download_url': f"https://dl.wenku8.com/down.php?type=txt&node={node}&id={book_id}",
                'page': book_url,
                'hot': '今日热门'
            }
        
        return title, info
    except Exception as e:
        _log.error(f"获取随机小说失败: {e}")
        return None, None

@register_command("/random_novel","/rn",help_text = "/random_novel 或者 /rn -> 发送随机小说",category = "6")
async def handle_random_novel(msg, is_group=True):
    # 检查 Cookie
    cookie_check = check_wenku8_cookie()
    if cookie_check:
        if is_group:
            await msg.reply(text=cookie_check)
        else:
            await bot.api.post_private_msg(msg.user_id, text=cookie_check)
        return
    
    # 从今日热门榜单获取随机小说
    novel, info = get_random_book_from_hotlist()
    
    # 如果URL获取失败，回退到JSON/内存字典
    if novel is None:
        _log.info("热门榜单获取失败，回退到JSON/内存字典")
        if books:
            novel = random.choice(list(books.keys()))
            info = books[novel]
            _log.info(f"从JSON/内存字典随机选择:《{novel}》")
        else:
            reply = "获取随机小说失败喵~请稍后再试~"
            if is_group:
                await msg.reply(text=reply)
            else:
                await bot.api.post_private_msg(msg.user_id, text=reply)
            return
    
    url = info["download_url"]
    hot = info.get('hot', '未知')
    
    # 创建HTML详情页
    cache_dir = os.path.join(load_address(), "novel_random")
    os.makedirs(cache_dir, exist_ok=True)
    file_token = hashlib.md5(f"random_{novel}_{time.time()}".encode("utf-8")).hexdigest()[:8]
    filename = f"{file_token}_{novel[:20]}.html"
    filepath = os.path.join(cache_dir, filename)
    
    build_novel_detail_html(novel, info, filepath)
    
    text_reply = f"抽选到了《{novel}》喵~\n"
    text_reply += f"简介如下喵~\n作者：{info['author']}\n字数：{info['word_count']}\n状态：{info['is_serialize']}\n热度：{hot}\n最新更新：{info['last_date']}\n简介：{info['introduction']}\n下载链接：{url}"
    
    if is_group:
        await msg.reply(text=text_reply)
        await bot.api.post_group_file(msg.group_id, file=filepath)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text_reply)
        await bot.api.upload_private_file(msg.user_id, filepath, filename)

@register_command("/hotnovel", help_text="/hotnovel <day|month> [数量] -> 获取今日/本月热门轻小说(支持翻页)", category="6")
async def handle_hotnovel(msg, is_group=True):
    # 检查 Cookie
    cookie_check = check_wenku8_cookie()
    if cookie_check:
        if is_group:
            await msg.reply(text=cookie_check)
        else:
            await bot.api.post_private_msg(msg.user_id, text=cookie_check)
        return
    
    parts = msg.raw_message.split()
    if len(parts) < 2:
        reply = "请输入查询类型喵~ 例如：/hotnovel day 或 /hotnovel month"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    rank_type = parts[1].lower()
    base_url = ""
    if rank_type == "day":
        base_url = "https://www.wenku8.net/modules/article/toplist.php?sort=dayvisit"
        type_name = "今日热门"
    elif rank_type in ["month", "mouth"]:
        base_url = "https://www.wenku8.net/modules/article/toplist.php?sort=monthvisit"
        type_name = "本月热门"
    else:
        reply = "目前只支持 day 或 month 喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    requested_count = 10
    if len(parts) >= 3:
        try:
            requested_count = int(parts[2])
            if requested_count <= 0:
                requested_count = 10
            if requested_count > 100:  # 设置一个合理的上限避免滥用
                requested_count = 100
        except ValueError:
            pass

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.wenku8.net/",
        "Cookie": WENKU8_COOKIE
    }

    all_matches = []
    current_page = 1

    try:
        while len(all_matches) < requested_count:
            url = f"{base_url}&page={current_page}"
            response = requests.get(url, headers=headers, timeout=10)
            response.encoding = 'gbk'
            content = response.text
            
            # 检查HTTP状态码
            if response.status_code == 403:
                _log.warning("热门榜单返回403错误，Cookie可能已失效或被反爬虫机制拦截")
                reply = "❌ 榜单获取失败，Cookie 可能已失效喵！\n请管理员使用 `/set_wenku_cookie <新Cookie>` 命令更新 Cookie 喵~"
                if is_group:
                    await msg.reply(text=reply)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=reply)
                return
            
            pattern = r'<div style="width:373px;height:136px;float:left;margin:5px 0px 5px 5px;">(.*?)</div>\s*</div>'
            page_matches = re.findall(pattern, content, re.DOTALL)
            
            if not page_matches:
                if current_page == 1:
                    # 检查是否需要登录（根据特定错误提示，排除已登录状态下的"退出登录"字样）
                    if "出现错误" in content or (("登录" in content and "退出登录" not in content) or "login" in content.lower()):
                        reply = "❌ 榜单获取失败，Cookie 可能已失效喵！\n请管理员使用 `/set_wenku_cookie <新Cookie>` 命令更新 Cookie 喵~"
                    else:
                        reply = "没找到热门榜单喵，可能网页结构变了喵~"
                    
                    if is_group:
                        await msg.reply(text=reply)
                    else:
                        await bot.api.post_private_msg(msg.user_id, text=reply)
                    return
                else:
                    # 如果不是第一页没结果，说明到底了
                    break
            
            all_matches.extend(page_matches)
            if len(page_matches) < 20: # 每一页通常是20个，如果少于20说明是最后一页
                break
            
            current_page += 1
            if current_page > 5: # 最多翻5页(100条)，防止无限循环
                break

        results = []
        for match in all_matches[:requested_count]:
            # 提取基本信息
            title_url_match = re.search(r'<b><a style="font-size:13px;" href="([^"]+)" title="([^"]+)" target="_blank">', match)
            book_url = title_url_match.group(1) if title_url_match else ""
            title = title_url_match.group(2) if title_url_match else "未知"
            
            # 提取 ID
            book_id = "0"
            id_match = re.search(r'/book/(\d+)\.htm', book_url)
            if id_match:
                book_id = id_match.group(1)
            
            node = int(book_id) // 1000
            
            # 提取其他详细信息以供 /info 使用
            author_cat_match = re.search(r'<p>作者:([^/]+)/分类:([^<]+)</p>', match)
            author = author_cat_match.group(1) if author_cat_match else "未知"
            category = author_cat_match.group(2) if author_cat_match else "未知"
            
            stats_match = re.search(r'<p>更新:([^/]+)/字数:([^/]+)/([^<]+)</p>', match)
            last_date = stats_match.group(1) if stats_match else "未知"
            word_count = stats_match.group(2) if stats_match else "未知"
            is_serialize = stats_match.group(3) if stats_match else "未知"
            
            tags_match = re.search(r'Tags:<span[^>]*>([^<]+)</span>', match)
            tags = tags_match.group(1) if tags_match else "无"
            
            intro_match = re.search(r'简介:([^<]+)', match)
            introduction = intro_match.group(1).strip() if intro_match else "暂无简介"
            
            img_match = re.search(r'<img src="([^"]+)"', match)
            cover_url = img_match.group(1) if img_match else f"https://img.wenku8.com/image/{node}/{book_id}/{book_id}s.jpg"

            # 构造下载链接
            download_url = f"https://dl.wenku8.com/down.php?type=txt&node={node}&id={book_id}"
            page_url = f"https://www.wenku8.net/book/{book_id}.htm"

            # 更新全局 books 字典
            books[title] = {
                "author": author,
                "category": category,
                "last_date": last_date,
                "word_count": word_count,
                "is_serialize": is_serialize,
                "introduction": introduction,
                "tags": tags,
                "cover_url": cover_url,
                "download_url": download_url,
                "page": page_url,
                "hot": "热门榜单书籍"
            }
            
            results.append((author, title, download_url))

        # 存储到临时选择列表
        temp_selections[msg.user_id] = results
        # 同时清空 api_book 避免干扰
        api_book[msg.user_id] = {}

        # 创建HTML文件
        cache_dir = os.path.join(load_address(), "novel_hot")
        os.makedirs(cache_dir, exist_ok=True)
        file_token = hashlib.md5(f"hotnovel_{rank_type}_{time.time()}".encode("utf-8")).hexdigest()[:8]
        filename = f"{file_token}_{type_name}.html"
        filepath = os.path.join(cache_dir, filename)

        build_novel_grid_html(f"{type_name} · 轻小说排行", filepath)

        # 添加到卡片
        for i, (author, title, download_url) in enumerate(results):
            book_id_match = re.search(r'id=(\d+)', download_url)
            book_id = book_id_match.group(1) if book_id_match else "0"
            append_novel_card(filepath, book_id, title, author, i + 1)

        close_novel_grid_html(filepath)

        if is_group:
            await msg.reply(text=f"✨ {type_name}前{len(results)}名喵~ 点击卡片查看详情或使用 /info 编号 获取信息喵~")
            await bot.api.post_group_file(msg.group_id, file=filepath)
        else:
            await bot.api.post_private_msg(msg.user_id, text=f"✨ {type_name}前{len(results)}名喵~ 点击卡片查看详情或使用 /info 编号 获取信息喵~")
            await bot.api.upload_private_file(msg.user_id, filepath, filename)

    except Exception as e:
        _log.error(f"Error in handle_hotnovel: {e}")
        reply = "获取热门榜单失败了喵，请稍后再试喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/res", help_text="/res <res值> -> 根据res编号下载轻小说", category="6")
async def handle_novel_by_res(msg, is_group=True):
    res_value = msg.raw_message[len("/res"):].strip()
    if not res_value:
        reply = "请输入要下载的res编号喵~例如：/res 1121"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    target_title = None
    target_info = None
    for title, info in books.items():
        if str(info.get("res")) == res_value:
            target_title = title
            target_info = info
            break

    if not target_info:
        download_url = f"https://dl.wenku8.com/down.php?type=txt&node=1&id={res_value}"
        reply = f"已开始下载res为 {res_value} 的轻小说喵~"
        if is_group:
            await msg.reply(text=reply)
            await bot.api.post_group_file(msg.group_id, file=download_url)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
            await bot.api.upload_private_file(msg.user_id, file=download_url, name=f"{res_value}.txt")
        return

    download_url = target_info.get("download_url")
    if not download_url:
        reply = "该轻小说没有可用的下载链接喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    reply = f"已开始下载《{target_title}》喵~"
    if is_group:
        await msg.reply(text=reply)
        await bot.api.post_group_file(msg.group_id, file=download_url)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)
        await bot.api.upload_private_file(msg.user_id, file=download_url, name=target_title + ".txt")

@register_command("/set_wenku_cookie", help_text="/set_wenku_cookie <Cookie> -> 更新文库8的Cookie(仅限管理员)", category="6", admin_show=True)
async def handle_set_wenku_cookie(msg, is_group=True):
    if str(msg.user_id) not in admin:
        reply = "主人，这个功能只有管理员才能使用喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    new_cookie = msg.raw_message[len("/set_wenku_cookie"):].strip()
    if not new_cookie:
        reply = "请输入新的 Cookie 喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    save_wenku8_cookie(new_cookie)
    reply = "✅ Cookie 更新成功喵！现在可以尝试使用 /hotnovel 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

mc = {}
@register_command("/mc",help_text = "/mc <服务器地址> -> 发送mc服务器状态",category = "3")
async def handle_mc(msg, is_group=True):
    if os.path.exists("mc.txt"):
        with open("mc.txt", "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) >= 2:
                    id = parts[0]
                    server = ":".join(parts[1:])
                    mc[id] = server
    else:
        with open("mc.txt", "w") as f:
            pass
    server = msg.raw_message[len("/mc"):].strip()
    if not server:
        if str(msg.user_id) in mc:
            server = mc[str(msg.user_id)]
        else:
            reply = "请输入服务器地址或使用/mc_bind进行绑定喵~"
            if is_group:
                await msg.reply(text=reply)
            else:
                await bot.api.post_private_msg(msg.user_id, text=reply)
            return
    
    try:
        import mcstatus
        server = mcstatus.JavaServer.lookup(server)
        status = server.status()
        reply = f"服务器状态如下喵~\n服务器描述：{status.description}\n版本: {status.version.name}\n在线人数: {status.players.online}\n最大人数: {status.players.max}\n延迟: {int(status.latency)}ms"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
    except ImportError:
        reply = "未安装mcstatus库喵~请使用pip install mcstatus进行安装喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
    except Exception:
        reply = "获取服务器状态失败喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/mc_bind",help_text = "/mc_bind <服务器地址> -> 绑定mc服务器",category = "3")
async def handle_mc_bind(msg, is_group=True):
    server = msg.raw_message[len("/mc_bind"):].strip()
    if not server:
        reply = "请输入服务器地址喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return
    mc[str(msg.user_id)] = server
    with open("mc.txt", "a") as f:
        f.write(f"{msg.user_id}:{server}\n")
    reply = "绑定成功喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/mc_unbind",help_text = "/mc_unbind -> 解绑mc服务器",category = "3")
async def handle_mc_unbind(msg, is_group=True):
    if str(msg.user_id) in mc:
        del mc[str(msg.user_id)]
        with open("mc.txt", "r") as f:
            lines = f.readlines()
        with open("mc.txt", "w") as f:
            for line in lines:
                if line.split(":")[0] != str(msg.user_id):
                    f.write(line)
        reply = "解绑成功喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
    else:
        reply = "你没有绑定过mc服务器喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/mc_show",help_text = "/mc_show -> 查看绑定的mc服务器",category = "3")
async def handle_mc_show(msg, is_group=True):
    if str(msg.user_id) in mc:
        reply = f"你绑定的mc服务器是：{mc[str(msg.user_id)]}"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
    else:
        reply = "你没有绑定过mc服务器喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/generate_photo","/gf",help_text = "/generate_photo 或 /gf <图片描述(不能有空格)> <大小> -> 生成图片",category = "3")
async def handle_gf(msg,is_group=True):
    text = re.sub(r'\[CQ:[^]]*\]', '', msg.raw_message).strip()
    prefix = "/generate_photo" if text.startswith("/generate_photo") else "/gf"
    default_size = "2k"
    try:
        args = text[len(prefix):].strip().split()
        if not args:
            raise ValueError
        if len(args) == 1:
            prompt = args[0]
            size = default_size
        else:
            size = args[-1]
            prompt = ' '.join(args[:-1])

        if ('x' not in size) and ('k' not in size) :
            size = default_size   
    except Exception:
        error_msg = f"请输入图片描述喵~ 格式: {prefix} <描述> [大小，默认{default_size}]"
        await (msg.reply(text=error_msg) if is_group else bot.api.post_private_msg(msg.user_id, text=error_msg))
        return
    
    if is_group:
        await msg.reply(text="正在绘制喵……")
    else:
        await bot.api.post_private_msg(msg.user_id,text="正在绘制喵……")
    
    if(msg.message[0]["type"] == "reply"):
        id = msg.message[0]["data"]["id"]
        msg_obj = await bot.api.get_msg(message_id=id)
        if msg_obj.get("data").get("message")[0].get("type") == "image": #处理图片
            image = msg_obj.get("data").get("message")[0].get("data").get("url")
    else:
        image = None


    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini')
    api_key = config_parser.get('gf', 'api_key')

    url = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

    payload = {
        "model": "doubao-seedream-4-0-250828",
        "prompt": prompt,
        "sequential_image_generation": "disabled",
        "response_format": "url",
        "size": size,
        "stream": False,
        "watermark": True
    }
    if(image):
        payload["image"] = image

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)

    try:
        url = response.json().get("data")[0].get("url")
    except Exception as e:
        reply = f"绘制失败喵~,{e}\n{response.json()}"

        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return
    if is_group:
        await msg.reply(text="绘制完成喵~")
        await bot.api.post_group_file(msg.group_id,image=url)
    else:
        await bot.api.post_private_msg(msg.user_id,text="绘制完成喵~")
        await bot.api.post_private_file(msg.user_id,image=url)

@register_command("/识别人物",help_text = "/识别人物 -> 识别图片中的二次元人物",category = "3")
async def handle_rec(msg, is_group=True):
    if is_group:
        await msg.reply(text="请先发送图片，再回复图片，加上@我，/识别人物")
    else:
        await bot.api.post_private_msg(msg.user_id,text="请先发送图片，再回复图片，加上/识别人物")
    return

@register_command("/at_all",help_text = "/at_all -> 识别@全体成员功能(admin)",category = "2",admin_show=True)
async def handle_at_all_group(msg, is_group=True):
    if is_group:
        if str(msg.user_id) not in admin:
            await msg.reply(text="只有管理员才能使用该命令喵~")
            return
        if str(msg.group_id) in at_all_group:
            at_all_group.remove(str(msg.group_id))
            write_at_all_group()
            await msg.reply(text="关闭成功喵~")
            return
        at_all_group.append(str(msg.group_id))
        write_at_all_group()
        await msg.reply(text="开启成功喵~")
    else:
        await bot.api.post_private_msg(msg.user_id,text="请在群聊中使用该命令")

# ========== 工作区命令 ==========
@register_command("/workspace", "/ws", help_text="/workspace 或 /ws -> 查看当前会话工作区文件列表", category="3")
async def handle_workspace(msg, is_group=True):
    """查看当前会话的工作区文件列表"""
    if not WORKSPACE_AVAILABLE:
        reply = "工作区功能不可用喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    from nbot.core.workspace import workspace_manager

    if is_group:
        session_id = get_qq_session_id(group_id=str(msg.group_id), group_user_id=str(msg.user_id))
    else:
        session_id = get_qq_session_id(user_id=str(msg.user_id))

    result = workspace_manager.list_files(session_id)
    files = result.get('files', [])

    if not files:
        reply = "当前工作区没有文件喵~"
    else:
        reply = f"工作区文件列表 ({len(files)} 个文件)：\n"
        for f in files:
            size_kb = f['size'] / 1024
            if size_kb < 1:
                size_str = f"{f['size']} B"
            elif size_kb < 1024:
                size_str = f"{size_kb:.1f} KB"
            else:
                size_str = f"{size_kb/1024:.2f} MB"
            reply += f"  {f['name']} ({size_str})\n"

    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/ws_send", help_text="/ws_send <文件名> -> 发送工作区中的文件", category="3")
async def handle_ws_send(msg, is_group=True):
    """发送工作区中的文件给用户"""
    if not WORKSPACE_AVAILABLE:
        reply = "工作区功能不可用喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    from nbot.core.workspace import workspace_manager

    # 解析文件名
    raw = msg.raw_message
    filename = raw[len("/ws_send"):].strip()
    if not filename:
        reply = "请指定文件名喵~ 用法: /ws_send 文件名"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    if is_group:
        session_id = get_qq_session_id(group_id=str(msg.group_id), group_user_id=str(msg.user_id))
    else:
        session_id = get_qq_session_id(user_id=str(msg.user_id))

    file_path = workspace_manager.get_file_path(session_id, filename)
    if not file_path:
        reply = f"文件不存在喵~: {filename}"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    file_path = normalize_file_path(file_path)
    try:
        if is_group:
            await bot.api.post_group_file(msg.group_id, file=file_path)
        else:
            await bot.api.upload_private_file(msg.user_id, file_path, os.path.basename(file_path))
    except Exception as e:
        reply = f"发送文件失败喵~: {e}"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)

#将help命令放在最后
@register_command("/help","/h",help_text = "/help 或者 /h -> 查看帮助",category = "8")
async def handle_help(msg, is_group=True):
    command_categories = {
        "1": {"name": "漫画相关"},
        "2": {"name": "聊天设置"},
        "3": {"name": "娱乐功能"},
        "4": {"name": "系统处理"},
        "5": {"name": "群聊管理"},
        "6": {"name": "轻小说"},
        "7": {"name": "定时任务"},
        "8": {"name": "全部功能"}
    }
    # 显示分类菜单
    if not msg.raw_message.strip().endswith("help") and not msg.raw_message.strip().endswith("h"):
        # 用户选择了分类
        selected_category = msg.raw_message.split()[-1]
        if selected_category in command_categories:
        # 显示该分类下的详细命令
            help_text = f"{command_categories[selected_category]['name']}命令喵~\n"
            if str(msg.user_id) in admin:
                command_categories = {
                    "1": {"name": "漫画相关", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "1"]},
                    "2": {"name": "聊天设置", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "2"]},
                    "3": {"name": "娱乐功能", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "3"]},
                    "4": {"name": "系统处理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "4"]},
                    "5": {"name": "群聊管理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "5"]},
                    "6": {"name": "轻小说", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "6"]},
                    "7": {"name": "定时任务", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "7"]}
                }
                # 添加全部功能分类
                command_categories["8"] = {
                    "name": "全部功能", 
                    "commands": [cmd for category in command_categories.values() for cmd in category["commands"]] + ["/help 或者 /h -> 查看帮助"]
                 }
                for cmd_text in command_categories[selected_category]['commands']:
                    help_text += f"{cmd_text}\n"
            else:
                command_categories = {
                    "1": {"name": "漫画相关", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "1" and handler.admin_show == False]},
                    "2": {"name": "聊天设置", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "2" and handler.admin_show == False]},
                    "3": {"name": "娱乐功能", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "3" and handler.admin_show == False]},
                    "4": {"name": "系统处理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "4" and handler.admin_show == False]},
                    "5": {"name": "群聊管理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "5" and handler.admin_show == False]},
                    "6": {"name": "轻小说", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "6" and handler.admin_show == False]},
                    "7": {"name": "定时任务", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "7" and handler.admin_show == False]}
                }
                # 添加全部功能分类
                command_categories["8"] = {
                    "name": "全部功能", 
                    "commands": [cmd for category in command_categories.values() for cmd in category["commands"]] + ["/help 或者 /h -> 查看帮助"]
                }
                if len(command_categories[selected_category]['commands']) == 0:
                    help_text += "你没有权限查看当前分类的命令喵~\n"

                for cmd_text in command_categories[selected_category]['commands']:
                    help_text += f"{cmd_text}\n"
                
            if is_group:
                await msg.reply(text=help_text)
            else:
                await bot.api.post_private_msg(msg.user_id, text=help_text)
            return

    # 显示主帮助菜单
    help_text = "欢迎使用喵~ 请选择分类查看详细命令喵~\n"
    for num, category in command_categories.items():
        help_text += f"{num}. {category['name']}\n"
    
    help_text += "\n输入 /help 或者 /h 加分类编号查看详细命令，例如: /help 1"

    help_text += "\n\n 一共有"+str(len(command_handlers))+"个命令"
    
    if is_group:
        await msg.reply(text=help_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=help_text)

def get_all_help_text_for_prompt() -> str:
    command_categories = {
        "1": {"name": "漫画相关", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "1"]},
        "2": {"name": "聊天设置", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "2"]},
        "3": {"name": "娱乐功能", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "3"]},
        "4": {"name": "系统处理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "4"]},
        "5": {"name": "群聊管理", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "5"]},
        "6": {"name": "轻小说", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "6"]},
        "7": {"name": "定时任务", "commands": [handler.help_text for handler in command_handlers.values() if handler.category == "7"]}
    }
    command_categories["8"] = {
        "name": "全部功能",
        "commands": [cmd for category in command_categories.values() for cmd in category["commands"]] + ["/help 或者 /h -> 查看帮助"]
    }
    help_text = "以下是全部命令：\n"
    for cmd_text in command_categories["8"]["commands"]:
        if cmd_text:
            help_text += f"{cmd_text}\n"
    help_text += "\n一共有"+str(len(command_handlers))+"个命令"
    return help_text

def parse_command_string(cmd_str):
    # 提取函数名和参数部分，支持带点的函数名
    func_match = re.match(r'^/([\w.]+)\((.*)\)$', cmd_str)
    if not func_match:
        return None
    
    func_name = func_match.group(1)
    params_str = func_match.group(2)
    
    # 解析参数
    params = {}
    for param in re.finditer(r'([\w.]+)\s*=\s*"([^"]*)"', params_str):
        key = param.group(1)
        value = param.group(2)
        params[key] = value
    
    return {
        'func': func_name,
        'params': params
    }

@register_command("/translate", "/tr", help_text="/translate <文本> -> 将文本翻译为中文/英文", category="3")
async def handle_translate(msg, is_group=True):
    text = msg.raw_message[len("/translate"):].strip() if msg.raw_message.startswith("/translate") else msg.raw_message[len("/tr"):].strip()
    if not text:
        reply = "请输入要翻译的文本喵~"
        if is_group: await msg.reply(text=reply)
        else: await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    try:
        response = ai_client.chat_completion(
            model=None,
            messages=[
                {"role": "system", "content": "你是一个专业的翻译官。如果输入是中文，请翻译成英文；如果输入是其他语言，请翻译成中文。只返回翻译结果，不要有任何多余的解释。"},
                {"role": "user", "content": text}
            ]
        )
        result = response.choices[0].message.content.strip()
        reply = f"翻译结果如下喵：\n{result}"
        if is_group: await msg.reply(text=reply)
        else: await bot.api.post_private_msg(msg.user_id, text=reply)
    except Exception as e:
        reply = f"翻译出错喵：{e}"
        if is_group: await msg.reply(text=reply)
        else: await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/fortune", "/jrrp", help_text="/fortune -> 查看今日运势", category="3")
async def handle_fortune(msg, is_group=True):
    user_id = str(msg.user_id)
    today = datetime.now().strftime("%Y%m%d")
    seed = int(user_id) + int(today)
    random.seed(seed)
    
    luck_score = random.randint(1, 100)
    
    fortunes = [
        "大吉：万事如意，心想事成喵！",
        "中吉：今天会有好事发生喵~",
        "小吉：平平安安就是福喵~",
        "末吉：虽然平淡，但也是充实的一天喵。",
        "凶：出门记得带伞，注意安全喵……",
        "大凶：建议今天宅在家里看漫画喵QAQ"
    ]
    
    if luck_score >= 90: fortune = fortunes[0]
    elif luck_score >= 70: fortune = fortunes[1]
    elif luck_score >= 50: fortune = fortunes[2]
    elif luck_score >= 30: fortune = fortunes[3]
    elif luck_score >= 10: fortune = fortunes[4]
    else: fortune = fortunes[5]
    
    # 恢复随机种子
    random.seed()
    
    reply = f"今日运势：{luck_score}点\n评价：{fortune}"
    if is_group: await msg.reply(text=reply)
    else: await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/bot",help_text="/bot.api.函数名(参数1=值1,参数2=值2) -> 用户自定义api(admin)，详情可见https://docs.ncatbot.xyz/guide/p8aun9nh/",category = "4",admin_show=True)
async def handle_api(msg,is_group):
    dict = parse_command_string(msg.raw_message)
    command = dict["func"]
    params = dict["params"]
    if command == "":
        return
    if str(msg.user_id) not in admin:
        text = "没有权限喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)
        return
    # 将命令字符串转换为bot.api中的方法
    try:
        func = getattr(bot.api, command.split('.')[-1])
        res = await func(**params)
        res = str(res)
        if is_group:
            await msg.reply(text=res)
        else:
            await bot.api.post_private_msg(msg.user_id, text=res)
    except Exception as e:
        text = f"执行命令时出错喵~：{e}"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)


async def handle_group_message(msg):
    """处理群消息"""
    is_group = True
    await dispatch_message(msg, is_group)


async def handle_private_message(msg):
    """处理私聊消息"""
    is_group = False
    await dispatch_message(msg, is_group)


# 获取机器人QQ号
config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')
BOT_UIN = str(config_parser.get('BotConfig', 'bot_uin', fallback=""))


def is_at_bot(msg) -> bool:
    """检查消息是否@了机器人"""
    # 方法1: 检查raw_message中是否包含@机器人的QQ号
    if BOT_UIN and BOT_UIN in msg.raw_message:
        return True
    
    # 方法2: 检查消息是否有at_list属性
    if hasattr(msg, 'at_list') and msg.at_list:
        for at in msg.at_list:
            if str(at) == BOT_UIN:
                return True
    
    # 方法3: 检查消息是否有at属性
    if hasattr(msg, 'at') and msg.at:
        if isinstance(msg.at, list):
            for at in msg.at:
                if str(at) == BOT_UIN:
                    return True
        elif str(msg.at) == BOT_UIN:
            return True
    
    return False


def _save_incoming_files_to_workspace(msg, is_group: bool):
    """检测消息中的文件并保存到工作区"""
    if not WORKSPACE_AVAILABLE:
        _log.debug("[文件保存] 工作区不可用")
        return
    from nbot.core.workspace import workspace_manager

    if is_group:
        session_id = get_qq_session_id(group_id=str(msg.group_id), group_user_id=str(msg.user_id))
        session_type = "qq_group"
    else:
        session_id = get_qq_session_id(user_id=str(msg.user_id))
        session_type = "qq_private"

    _log.info(f"[文件保存] 开始检查消息，session_id={session_id}, type={session_type}")

    # 方法1: 检查 msg.message 中的文件元素
    if hasattr(msg, 'message') and msg.message:
        _log.info(f"[文件保存] 消息元素数量: {len(msg.message)}")

        for elem in msg.message:
            if not hasattr(elem, 'type'):
                _log.debug(f"[文件保存] 元素没有type属性: {elem}")
                continue
            
            _log.info(f"[文件保存] 检查元素类型: {elem.type}")
            
            # 处理文件类型消息
            if elem.type == 'file':
                file_url = elem.data.get('url', '') if hasattr(elem, 'data') else ''
                file_name = elem.data.get('name', 'unknown_file') if hasattr(elem, 'data') else 'unknown_file'
                _log.info(f"[文件保存] 发现文件: {file_name}, URL: {file_url[:50] if file_url else '空'}...")
                if file_url:
                    try:
                        resp = requests.get(file_url, timeout=30)
                        _log.info(f"[文件保存] 下载响应: {resp.status_code}")
                        if resp.status_code == 200:
                            result = workspace_manager.save_uploaded_file(
                                session_id, resp.content, file_name, session_type)
                            _log.info(f"[文件保存] 保存成功: {result}")
                        else:
                            _log.warning(f"[文件保存] 下载失败，状态码: {resp.status_code}")
                    except Exception as e:
                        _log.warning(f"[文件保存] 保存失败: {file_name}, {e}", exc_info=True)
            else:
                _log.debug(f"[文件保存] 非文件类型元素: {elem.type}")
    
    # 方法2: 从 raw_message 解析 CQ 码格式的文件
    if hasattr(msg, 'raw_message') and msg.raw_message:
        raw_msg = msg.raw_message
        _log.info(f"[文件保存] 检查 raw_message: {raw_msg[:100]}...")
        
        # 匹配 [CQ:file,file=文件名,file_id=xxx,url=xxx] 格式
        file_cq_pattern = r'\[CQ:file[^,]*,file=([^,\]]+)(?:[^,]*,file_id=([^,\]]+))?(?:[^,]*,url=([^,\]]+))?[^\]]*\]'
        file_matches = re.findall(file_cq_pattern, raw_msg)
        
        if file_matches:
            _log.info(f"[文件保存] 从 CQ 码发现 {len(file_matches)} 个文件")
            for match in file_matches:
                file_name = match[0] if match[0] else 'unknown_file'
                file_id = match[1] if len(match) > 1 and match[1] else None
                file_url = match[2] if len(match) > 2 and match[2] else None
                
                _log.info(f"[文件保存] CQ码文件: {file_name}, file_id={file_id}, url={file_url[:50] if file_url else '无'}...")
                
                # 如果没有 URL，尝试从 file_id 获取文件下载链接
                if not file_url and file_id:
                    # 使用 OneBot HTTP API 获取文件下载链接
                    try:
                        # 构造 OneBot API 请求
                        # 参考: https://github.com/botuniverse/onebot-11/blob/master/api/public.md#get_file-
                        api_url = "http://127.0.0.1:3000"  # 默认 OneBot HTTP 地址
                        if is_group:
                            endpoint = f"{api_url}/get_group_file_url"
                            params = {"group_id": msg.group_id, "file_id": file_id}
                        else:
                            endpoint = f"{api_url}/get_private_file_url"
                            params = {"user_id": msg.user_id, "file_id": file_id}
                        
                        _log.info(f"[文件保存] 尝试获取文件URL: {endpoint}")
                        
                        # 尝试使用 bot.api 的底层方法
                        if hasattr(bot.api, '_request') or hasattr(bot.api, 'request'):
                            # 尝试调用 OneBot API
                            request_method = getattr(bot.api, '_request', getattr(bot.api, 'request', None))
                            if request_method:
                                file_info = request_method(endpoint, params=params)
                                if file_info and 'data' in file_info and 'url' in file_info['data']:
                                    file_url = file_info['data']['url']
                                    _log.info(f"[文件保存] 通过API获取到文件URL: {file_url[:50]}...")
                    except Exception as e:
                        _log.warning(f"[文件保存] 获取文件下载链接失败: {e}")
                    
                    # 如果上面的方法失败，尝试直接通过 file_id 下载
                    if not file_url:
                        _log.info("[文件保存] 尝试直接使用 file_id 下载")
                        # 某些协议支持直接通过 file_id 下载
                        try:
                            # 尝试从 ncatbot 配置中获取基础 URL
                            from nbot.services.ai import base_url
                            if base_url:
                                # 构造可能的下载链接
                                possible_urls = [
                                    f"{base_url}/download_file?file_id={file_id}",
                                    f"{base_url}/get_file?file_id={file_id}",
                                ]
                                for url in possible_urls:
                                    try:
                                        resp = requests.head(url, timeout=5)
                                        if resp.status_code == 200:
                                            file_url = url
                                            _log.info(f"[文件保存] 找到可用下载链接: {file_url[:50]}...")
                                            break
                                    except:
                                        continue
                        except Exception as e:
                            _log.debug(f"[文件保存] 尝试直接下载失败: {e}")
                
                if file_url:
                    try:
                        resp = requests.get(file_url, timeout=30)
                        _log.info(f"[文件保存] 下载响应: {resp.status_code}")
                        if resp.status_code == 200:
                            result = workspace_manager.save_uploaded_file(
                                session_id, resp.content, file_name, session_type)
                            _log.info(f"[文件保存] 保存成功: {result}")
                        else:
                            _log.warning(f"[文件保存] 下载失败，状态码: {resp.status_code}")
                    except Exception as e:
                        _log.warning(f"[文件保存] 保存失败: {file_name}, {e}", exc_info=True)
                else:
                    _log.warning(f"[文件保存] 无法获取文件下载链接: {file_name}")
        else:
            _log.debug("[文件保存] raw_message 中没有 CQ:file 码")


async def dispatch_message(msg, is_group: bool):
    """分发消息到命令处理器"""
    raw_msg = msg.raw_message

    if not raw_msg:
        return

    # 获取图片 URL - 优先使用 msg.message[0].data.url
    image_url = None
    try:
        if hasattr(msg, 'message') and msg.message:
            first_elem = msg.message[0]
            if hasattr(first_elem, 'type') and first_elem.type == 'image':
                image_url = first_elem.data.get('url')
                if image_url:
                    _log.info(f"从 message[0].data.url 获取图片 URL: {image_url[:50]}...")
    except Exception as e:
        _log.warning(f"无法从 message 获取图片 URL: {e}")

    # 如果上面获取失败，尝试解析 CQ 码
    if not image_url:
        import re
        image_match = re.search(r'\[CQ:image[^,]*url=(https?://[^,\]]+)', raw_msg)
        if image_match:
            image_url = image_match.group(1)
            _log.info(f"从 CQ 码解析图片 URL: {image_url[:50]}...")

    # 检测并保存用户上传的文件到工作区
    if WORKSPACE_AVAILABLE:
        try:
            _save_incoming_files_to_workspace(msg, is_group)
        except Exception as e:
            _log.warning(f"保存文件到工作区失败: {e}")

    # 检查是否是命令
    for commands, handler in command_handlers.items():
        for cmd in commands:
            if raw_msg.startswith(cmd):
                try:
                    await handler(msg, is_group)
                except Exception as e:
                    _log.error(f"Error handling command {cmd}: {e}")
                return

    # AI 聊天回复
    loop = asyncio.get_event_loop()

    if is_group:
        # 群聊：只有@bot时才回复
        if not is_at_bot(msg):
            _log.debug(f"Group message ignored (not @bot): {raw_msg[:50]}...")
            return
        
        from nbot.services.chat_service import chat as do_chat
        try:
            content = raw_msg
            group_id = str(msg.group_id) if hasattr(msg, 'group_id') else None
            user_id = str(msg.user_id) if hasattr(msg, 'user_id') else None
            _log.info(f"Processing group message (at bot) from {user_id} in {group_id}: {content[:50]}..., image: {bool(image_url)}")
            response = await loop.run_in_executor(None, do_chat, content, None, group_id, user_id, bool(image_url), image_url, None)
            if response:
                _log.info(f"Sending group reply: {response[:50]}...")
                await msg.reply(text=response)
            else:
                _log.warning("No response from chat service")
        except Exception as e:
            _log.error(f"Error in group chat: {e}")
            import traceback
            _log.error(traceback.format_exc())
    else:
        from nbot.services.chat_service import chat as do_chat
        try:
            content = raw_msg
            user_id = str(msg.user_id) if hasattr(msg, 'user_id') else None
            _log.info(f"Processing private message from {user_id}: {content[:50]}..., image: {bool(image_url)}")
            response = await loop.run_in_executor(None, do_chat, content, user_id, None, None, bool(image_url), image_url, None)
            if response:
                _log.info(f"Sending private reply: {response[:50]}...")
                await msg.reply(text=response)
            else:
                _log.warning("No response from chat service")
        except Exception as e:
            _log.error(f"Error in private chat: {e}")
            import traceback
            _log.error(traceback.format_exc())


# 防止重复注册事件处理器
if not hasattr(bot, '_nbot_handlers_registered'):
    bot.add_group_event_handler(handle_group_message)
    bot.add_private_event_handler(handle_private_message)
    bot._nbot_handlers_registered = True
