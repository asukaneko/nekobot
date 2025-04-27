from ncatbot.core import BotClient, GroupMessage, PrivateMessage,MessageChain,Music
from config import load_config
from chat import group_messages, user_messages, tts, chat
import jmcomic,requests,random,configparser,json,yaml,re,os,asyncio
from jmcomic import *
from typing import Dict, List
from datetime import datetime

#----------------------
# region 全局变量设置
#----------------------

if_tts = False #判断是否开启TTS

bot_id,admin_id = load_config() # 加载配置,返回机器人qq号

bot = BotClient()

command_handlers = {}

user_favorites: Dict[str, List[str]] = {}  # 用户收藏夹 {user_id: [comic_ids]}
group_favorites: Dict[str, Dict[str, List[str]]] = {}  # 群组收藏夹 {group_id: {user_id: [comic_ids]}}

admin = [str(admin_id)]  # 确保admin_id是字符串形式

blak_list_comic = {"global": [], "groups": {}, "users": {}} # str,黑名单

# ------------------
# region 通用函数
# ------------------

def write_admin():
    try:
        with open("admin.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(admin) + "\n")  # 每行一个管理员ID
    except Exception as e:
        print(f"写入管理员文件失败: {e}")

def load_admin():
    try:
        with open("admin.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line != str(admin_id):
                    admin.append(line)
    except FileNotFoundError:
        write_admin()

def register_command(*command,help_text = None): # 注册命令
    """
    装饰器，用于注册命令。
    :param command: 命令名称，支持多个。
    :param help_text: 命令的帮助文本。
    """
    def decorator(func):
        command_handlers[command] = func
        if help_text is not None:
            func.help_text = help_text
        return func
    return decorator

def load_address(): # 加载配置文件，返回图片保存地址
    with open("option.yml", "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
        after_photo_list = conf.get("plugins", {}).get("after_photo", [])
        if after_photo_list and isinstance(after_photo_list, list):
            pdf_dir = after_photo_list[0].get("kwargs", {}).get("pdf_dir", "./cache/pdf/")
        else:
            pdf_dir = "./cache/pdf/"
        if not pdf_dir.endswith(os.path.sep):
            pdf_dir += os.path.sep
        return pdf_dir

def load_favorites():
    """加载收藏夹数据"""
    cache_dir = load_address()[:-4] + "list/"
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

def save_favorites():
    """保存收藏夹数据"""
    cache_dir = load_address()[:-4] + "list/"
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

async def chatter(msg):
    """
    定时聊天函数。
    :param msg: 消息对象。
    """
    content = chat(content="现在请你根据上下文，主动和用户聊天",user_id=msg.user_id)
    if if_tts:
        rtf = tts(content)
        await bot.api.post_private_msg(msg.user_id, rtf=rtf)
        await bot.api.post_private_msg(msg.user_id, text=content)
    else:
        await bot.api.post_private_msg(msg.user_id, text=content)

async def write_blak_list():
    """
    写入黑名单
    """
    cache_dir = load_address()[:-4] + "black_list/"
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"blak_list.json"), "w", encoding="utf-8") as f:
            json.dump(blak_list_comic, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"写入黑名单文件失败: {e}")

async def load_blak_list():
    """
    加载黑名单
    """
    cache_dir = load_address()[:-4] + "black_list/"
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"blak_list.json"), "r", encoding="utf-8") as f:
            blak_list_comic.update(json.load(f))

    except FileNotFoundError:
        await write_blak_list()
#---------------------------------------------------------------------------

load_favorites()
load_admin()
load_blak_list()

#----------------------
#     region 命令
#----------------------

@register_command("测试")
async def handle_test(msg, is_group=True):
    if not msg.raw_message == "测试":
        return
    reply_text = "测试成功喵~\n输入 /help 查看帮助喵~"
    if is_group:
        await msg.reply(text=reply_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply_text)

@register_command("/tts",help_text = "/tts -> 开启或关闭TTS")
async def handle_tts(msg, is_group=True):
    global if_tts
    if_tts = not if_tts
    text = "已开启TTS喵~" if if_tts else "已关闭TTS喵~"
    if is_group:
        await msg.reply(text=text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=text)

#漫画类命令----------------
@register_command("/jmrank",help_text = "/jmrank <月排行/周排行> -> 获取排行榜")
async def handle_jmrank(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在获取排行喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在获取排行喵~")
    select = msg.raw_message[len("/jmrank"):].strip()

    # 创建客户端
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

    cache_dir = load_address()[:-4]
    cache_dir += "rank/"
    os.makedirs(cache_dir,exist_ok = True)

    name = time.time()
    tot = 0
    for page in cl.categories_filter_gen(page=1,  # 起始页码
                                         # 下面是分类参数
                                         time=JmMagicConstants.TIME_WEEK,
                                         category=JmMagicConstants.CATEGORY_ALL,
                                         order_by=JmMagicConstants.ORDER_BY_VIEW,
                                         ):
        for aid, atitle in page:
            tot += 1
            with open(cache_dir + f"{select}_{name}.txt", "a", encoding="utf-8") as f:
                f.write(f"{tot}: {aid}  {atitle}\n\n")

    if is_group:
        await bot.api.post_group_file(msg.group_id, file=cache_dir + f"{select}_{name}.txt")
    else:
        await bot.api.upload_private_file(msg.user_id, cache_dir + f"{select}_{name}.txt", f"{select}_{name}.txt")

@register_command("/search",help_text = "/search <内容> -> 搜索漫画")
async def handle_search(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = load_address()[:-4]
    cache_dir += "search/"
    os.makedirs(cache_dir,exist_ok = True)

    client = JmOption.default().new_jm_client()

    content = msg.raw_message[len("/search"):].strip()
    
    if not content or content == " ":
        if is_group:
            await msg.reply(text="搜索内容不能为空喵~")
        else:
            await bot.api.post_private_msg(msg.user_id, text="搜索内容不能为空喵~")
        return
        
    if re.match(r'^\d+$', content):  # 检查是否为纯数字
        id = content
        # 直接搜索禁漫车号
        page = client.search_site(search_query=id)
        album: JmAlbumDetail = page.single_album
        with open(cache_dir + f"{id}.txt", "w", encoding="utf-8") as f:
            f.write(f"标题：{album.title}\n标签：{album.tags}\n页数：{album.page_count}\n浏览次数：{album.views}\n评论数：{album.comment_count}")
        if is_group:
            await bot.api.post_group_file(msg.group_id, file=cache_dir + f"{id}.txt")
        else:
            await bot.api.upload_private_file(msg.user_id, cache_dir + f"{id}.txt", f"{id}.txt")
        return

    name = content + str(time.time()).replace(".", "")
    
    with open(cache_dir + f"{name}.txt", "w", encoding="utf-8") as f:
        f.write(f"搜索结果：{content}\n")
    tot = 0
    for i in range(5):# 搜索5页，可以自己修改
        page: JmSearchPage = client.search_site(search_query=content, page=i+1,order_by=JmMagicConstants.ORDER_BY_VIEW)
        for album_id, title in page:
            tot += 1
            with open(cache_dir + f"{name}.txt", "a", encoding="utf-8") as f:
                f.write(f"{tot}: {album_id}  {title}\n\n")
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=cache_dir + f"{name}.txt")
    else:
        await bot.api.upload_private_file(msg.user_id, cache_dir + f"{name}.txt", f"{content}.txt")


@register_command("/tag",help_text = "/tag <标签> -> 搜索漫画标签")
async def handle_search(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = load_address()[:-4]
    cache_dir += "search/"
    os.makedirs(cache_dir,exist_ok = True)

    content = msg.raw_message[len("/tag"):].strip()
    name = content + str(time.time()).replace(".", "")
    client = JmOption.default().new_jm_client()
    with open(cache_dir + f"{name}.txt", "w", encoding="utf-8") as f:
        f.write(f"搜索标签结果：{content}\n")
    tot = 0
    for i in range(5):# 搜索5页，可以自己修改
        page: JmSearchPage = client.search_tag(search_query=content, page=i+1,order_by=JmMagicConstants.ORDER_BY_VIEW)
        for album_id, title in page:
            tot += 1
            with open(cache_dir + f"{name}.txt", "a", encoding="utf-8") as f:
                f.write(f"{tot}: {album_id}  {title}\n\n")
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=cache_dir + f"{name}.txt")
    else:
        await bot.api.upload_private_file(msg.user_id, cache_dir + f"{name}.txt", f"{content}.txt")

@register_command("/get_fav",help_text = "/get_fav <用户名> <密码> -> 获取收藏夹")
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
    # 注意，这里的密码是明文显示的，所以需要确保安全性，群聊中最好不要使用
    # 使用提取的用户名和密码进行后续操作
    if is_group:
        await msg.reply(text="正在获取收藏夹喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在获取收藏夹喵~")

    cache_dir = load_address()[:-4]
    cache_dir += "fav/"
    os.makedirs(cache_dir, exist_ok=True)

    print(username,password)

    name = username + str(time.time()).replace(".", "")

    option = JmOption.default()
    cl = option.new_jm_client()
    try:
        cl.login(username, password)# 也可以使用login插件/配置cookies
    except Exception as e:
        if is_group:
            await msg.reply(text=f"登录失败喵~：{e}")
        else:
            await bot.api.post_private_msg(msg.user_id, text=f"登录失败喵~：{e}")
        return

    # 遍历全部收藏的所有页
    for page in cl.favorite_folder_gen():  # 如果你只想获取特定收藏夹，需要添加folder_id参数
        # 遍历每页结果
        for aid, atitle in page.iter_id_title():
            with open(cache_dir + f"{name}.txt", "a", encoding="utf-8") as f:
                f.write(f"{aid}  {atitle}\n\n")

    if is_group:
        await bot.api.post_group_file(msg.group_id, file=cache_dir + f"{name}.txt")
    else:
        await bot.api.upload_private_file(msg.user_id, cache_dir + f"{name}.txt", f"{username}.txt")


@register_command("/jm",help_text = "/jm <漫画ID> -> 下载漫画")
async def handle_jmcomic(msg, is_group=True):
    match = re.match(r'^/jm\s+(\d+)$', msg.raw_message)
    if match:
        comic_id = match.group(1)

        # 检查是否在全局、群组或用户黑名单中
        if comic_id in blak_list_comic["global"]:
            error_msg = "该漫画已被加入黑名单喵~"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
            return
        if is_group:
            group_id = str(msg.group_id)
            if group_id in blak_list_comic["groups"] and comic_id in blak_list_comic["groups"][group_id]:
                error_msg = "该漫画已被加入本群黑名单喵~"
                await msg.reply(text=error_msg)
                return
        else:
            user_id = str(msg.user_id)
            if user_id in blak_list_comic["users"] and comic_id in blak_list_comic["users"][user_id]:
                error_msg = "该漫画已被加入你的黑名单喵~"
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
                return

        # 立即回复用户，不等待下载完成
        reply_text = f"已开始下载漫画ID：{comic_id}，下载完成后会自动通知喵~"
        if is_group:
            await msg.reply(text=reply_text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply_text)

        # 创建后台任务
        asyncio.create_task(download_and_send_comic(comic_id, msg, is_group))
    else:
        error_msg = "格式错误了喵~，请输入 /jm 后跟漫画ID"
        if not is_group:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

# 新增后台任务函数
async def download_and_send_comic(comic_id, msg, is_group):
    try:
        # 在线程池中执行阻塞操作
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda:
            jmcomic.download_album(
                comic_id,
                jmcomic.create_option_by_file('./option.yml')
            )
        )

        pdf_dir = load_address()
        file_path = os.path.join(pdf_dir, f"{comic_id}.pdf")

        # 检查文件是否真正生成
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"PDF文件未生成：{file_path}")

        success_text = f"漫画 {comic_id} 下载完成喵~"
        if is_group:
            await bot.api.post_group_file(msg.group_id, file=file_path)
            await msg.reply(text=success_text)
        else:
            await bot.api.upload_private_file(msg.user_id, file_path, f"{comic_id}.pdf")
            await bot.api.post_private_msg(msg.user_id, text=success_text)

    except Exception as e:
        error_msg = f"下载失败喵~: {str(e)}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

# ====下面的收藏夹不是官方的收藏夹，是本地储存的====
@register_command("/add_fav", help_text="/add_fav <漫画ID> -> 添加收藏")
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

@register_command("/list_fav", help_text="/list_fav -> 查看收藏列表")
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

@register_command("/del_fav", help_text="/del_fav <漫画ID> -> 删除收藏")
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

@register_command("/add_black_list","/abl",help_text = "/add_black_list 或 /abl  <漫画ID> -> 添加黑名单")
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
            if group_id not in blak_list_comic["groups"]:
                blak_list_comic["groups"][group_id] = []
            if comic_id in blak_list_comic["groups"][group_id]:
                reply = f"漫画 {comic_id} 已在本群黑名单中喵~"
            else:
                blak_list_comic["groups"][group_id].append(comic_id)
                await write_blak_list()
                reply = f"已在本群黑名单中添加漫画 {comic_id} 喵~"
        else:
            user_id = str(msg.user_id)
            if user_id not in blak_list_comic["users"]:
                blak_list_comic["users"][user_id] = []
            if comic_id in blak_list_comic["users"][user_id]:
                reply = f"漫画 {comic_id} 已在你的黑名单中喵~"
            else:
                blak_list_comic["users"][user_id].append(comic_id)
                await write_blak_list()
                reply = f"已在你的黑名单中添加漫画 {comic_id} 喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/del_black_list","/dbl",help_text = "/del_black_list 或 /dbl <漫画ID> -> 删除黑名单")
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
            if group_id in blak_list_comic["groups"] and comic_id in blak_list_comic["groups"][group_id]:
                blak_list_comic["groups"][group_id].remove(comic_id)
                await write_blak_list()
                reply = f"已从本群黑名单中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在本群黑名单中喵~"
        else:
            user_id = str(msg.user_id)
            if user_id in blak_list_comic["users"] and comic_id in blak_list_comic["users"][user_id]:
                blak_list_comic["users"][user_id].remove(comic_id)
                await write_blak_list()
                reply = f"已从你的黑名单中删除漫画 {comic_id} 喵~"
            else:
                reply = f"漫画 {comic_id} 不在你的黑名单中喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/list_black_list","/lbl",help_text = "/list_black_list 或 /lbl -> 查看黑名单")
async def handle_list_black_list(msg, is_group=True):
    if is_group:
        group_id = str(msg.group_id)
        black_list = blak_list_comic["global"] + blak_list_comic["groups"].get(group_id, [])
        if black_list:
            reply = "本群的黑名单中的漫画ID:\n" + "\n".join(black_list)
        else:
            reply = "本群黑名单是空的喵~"
    else:
        user_id = str(msg.user_id)
        black_list = blak_list_comic["global"] + blak_list_comic["users"].get(user_id, [])
        if black_list:
            reply = "你的黑名单中的漫画ID:\n" + "\n".join(black_list)
        else:
            reply = "你的黑名单是空的喵~"
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)
        
#------------------------

@register_command("/set_prompt","/sp",help_text = "/set_prompt 或者 /sp <提示词> -> 设定提示词")
async def handle_set_prompt(msg, is_group=True):
    prompt_content = ""
    if msg.raw_message.startswith("/set_prompt"):
        prompt_content = msg.raw_message[len("/set_prompt"):].strip()
    elif msg.raw_message.startswith("/sp"):
        prompt_content = msg.raw_message[len("/sp"):].strip()
    id_str = str(msg.group_id if is_group else msg.user_id)
    os.makedirs("prompts", exist_ok=True)

    prefix = "group" if is_group else "user"
    with open(f"prompts/{prefix}/{prefix}_{id_str}.txt", "w", encoding="utf-8") as file:
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


@register_command("/del_prompt","/dp",help_text = "/del_prompt 或者 /dp -> 删除提示词")
async def handle_del_prompt(msg, is_group=True):
    id_str = str(msg.group_id if is_group else msg.user_id)
    if is_group:
        if id_str in group_messages:
            del group_messages[id_str]
            try:
                os.remove(f"prompts/group/group_{id_str}.txt")
                with open("neko.txt", "r", encoding="utf-8") as file:
                    prompt = file.read()
                    group_messages[id_str] = [{"role": "system", "content": prompt}]
                await msg.reply(text="提示词已删除喵~本子娘回来了喵~")
            except FileNotFoundError:
                await msg.reply(text="没有可以删除的提示词喵~")

    else:
        if id_str in user_messages:
            del user_messages[id_str]
            try:
                os.remove(f"prompts/user/user_{id_str}.txt")
                with open("neko.txt", "r", encoding="utf-8") as file:
                    prompt = file.read()
                    user_messages[id_str] = [{"role": "system", "content": prompt}]
                await bot.api.post_private_msg(msg.user_id, text="提示词已删除喵~本子娘回来了喵~")
            except FileNotFoundError:
                await bot.api.post_private_msg(msg.user_id, text="没有可以删除的提示词喵~")

@register_command("/get_prompt","/gp",help_text = "/get_prompt 或者 /gp -> 获取提示词")
async def handle_get_prompt(msg, is_group=True):
    id_str = str(msg.group_id if is_group else msg.user_id)
    if is_group:
        try:
            with open(f"prompts/group/group_{id_str}.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
                await msg.reply(text=prompt)
        except FileNotFoundError:
            await msg.reply(text="没有找到提示词喵~")
    else:
        try:
            with open(f"prompts/user/user_{id_str}.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
                await bot.api.post_private_msg(msg.user_id, text=prompt)
        except FileNotFoundError:
            await bot.api.post_private_msg(msg.user_id, text="没有找到提示词喵~")


@register_command("/agree","/agree -> 同意好友请求") # 同意好友请求
async def handle_agree(msg, is_group=True):
    if not is_group:
        await bot.api.set_friend_add_request(flag=msg.user_id, approve=True,remark=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, text="已同意好友请求喵~")
    else:
        await bot.api.set_friend_add_request(flag=msg.user_id, approve=True,remark=msg.user_id)
        await msg.reply(text="已同意好友请求喵~")


@register_command("/restart","/restart -> 重启机器人")
async def handle_restart(msg, is_group=True):
    reply_text = "正在重启喵~"
    if is_group:
        await msg.reply(text=reply_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply_text)
    # 重启逻辑
    os.execv(sys.executable, [sys.executable] + sys.argv)

#------以下为调用api发送文件的命令，采用异步方式发送文件------
# 新增后台任务函数
async def async_send_file(send_method, target_id, file_type, url):
    try:
        # 处理可能的重定向
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: requests.get(url, allow_redirects=True, timeout=10))
        final_url = response.url

        # 异步发送文件
        await send_method(target_id, **{file_type: final_url})
    except Exception as e:
        pass

# 修改通用处理函数
async def handle_generic_file(msg, is_group: bool, section: str, file_type: str, custom_url: str = None):
    """通用文件处理函数（修复版）"""
    """
        支持的file_type:
        image: 图片
        record: 语音
        video: 视频
        file: 文件
        markdown: Markdown
    """
    # 立即回复用户
    initial_text = "正在获取喵~"
    if initial_text:
        if is_group:
            await msg.reply(text=initial_text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=initial_text)

    try:
        # 修复配置读取逻辑
        if section:  # 仅当需要读取配置文件时
            loop = asyncio.get_event_loop()
            # 正确读取配置的方式
            def read_config():
                cfg = configparser.ConfigParser()
                cfg.read('urls.ini')
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
        asyncio.create_task(
            async_send_file(send_method, target_id, file_type, selected_url)
        )

    except Exception as e:
        error_msg = f"配置错误喵~: {str(e)}" if '配置' in str(e) else f"获取失败喵~: {str(e)}"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

# 统一调用
@register_command("/random_image","/ri",help_text = "/random_image 或者 /ri -> 随机图片")
async def handle_random_image(msg, is_group=True):
    await handle_generic_file(msg, is_group, 'ri', 'image')

@register_command("/random_emoticons","/re",help_text = "/random_emoticons 或者 /re -> 随机表情包")
async def handle_random_emoticons(msg, is_group=True):
    await handle_generic_file(msg, is_group, 're', 'image')

@register_command("/st",help_text = "/st <标签名> -> 发送随机涩图,标签支持与或(& |)")
async def handle_st(msg, is_group=True):
    tags = msg.raw_message[len("/st"):].strip()
    res = requests.get(f"https://api.lolicon.app/setu/v2?tag={tags}").json().get("data")[0].get("urls").get("original")
    await handle_generic_file(msg, is_group,"","image",custom_url=res)  # 特殊处理API调用

@register_command("/random_video","/rv",help_text = "/random_video 或者 /rv -> 随机二次元视频")
async def handle_random_video(msg, is_group=True):
    await handle_generic_file(msg, is_group, 'rv', 'video')

#---------------------------------------------

@register_command("/music","/m",help_text = "/music <音乐名/id> -> 发送音乐")
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

@register_command("/random_dice","/rd",help_text = "/random_dice 或者 /rd -> 发送随机骰子")
async def handle_random_dice(msg, is_group=True):
    if is_group:
        await bot.api.post_group_msg(msg.group_id,dice=True)
    else:
        await bot.api.post_private_msg(msg.user_id,dice=True)

@register_command("/random_rps","/rps",help_text = "/random_rps 或者 /rps -> 发送随机石头剪刀布")
async def handle_random_rps(msg, is_group=True):
    if is_group:
        await bot.api.post_group_msg(msg.group_id,rps=True)
    else:
        await bot.api.post_private_msg(msg.user_id,rps=True)

@register_command("/del_message","/dm",help_text = "/del_message 或者 /dm -> 删除对话记录")
async def handle_del_message(msg, is_group=True):
    if is_group:
        del group_messages[str(msg.group_id)]
        await msg.reply(text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")
    else:
        del user_messages[str(msg.user_id)]
        await bot.api.post_private_msg(msg.user_id, text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")

@register_command("/remind",help_text="/remind <时间(小时)> <内容> -> 定时提醒")
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

@register_command("/premind", help_text="/premind <MM-DD> <HH:MM> <内容> -> 精确时间提醒")
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
            
    except ValueError as e:
        error_msg = "格式错误喵~ 使用: /precise_remind MM-DD HH:MM 提醒内容"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

@register_command("/set_admin","/sa",help_text = "/set_admin <qq号> 或者 /sa <qq号> -> 设置管理员(root)")
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

@register_command("/del_admin","/da",help_text = "/del_admin <qq号> 或者 /da <qq号> -> 删除管理员(root)")
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

@register_command("/get_admin","/ga",help_text = "/get_admin 或者 /ga -> 获取管理员")
async def handle_get_admin(msg, is_group=True):
    if is_group:
        await msg.reply(text="管理员列表："+str(admin))
    else:
        await bot.api.post_private_msg(msg.user_id, text="管理员列表："+str(admin))


@register_command("/set_ids",help_text = "/set_ids <昵称> <个性签名> <性别> -> 设置账号信息(管理员)")
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
    except Exception as e:
        text = "设置失败喵~"
        if is_group:
            await msg.reply(text=text)
        else:
            await bot.api.post_private_msg(msg.user_id, text=text)

@register_command("/set_online_status",help_text = "/set_online_status <在线状态> -> 设置在线状态(管理员)")
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

@register_command("/get_friends",help_text = "/get_friends -> 获取好友列表（管理员）")
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

@register_command("/set_qq_avatar",help_text = "/set_qq_avatar <地址> -> 更改头像（管理员）")
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

@register_command("/send_like",help_text = "/send_like <目标QQ号> <次数> -> 发送点赞")
async def handle_send_like(msg, is_group=True):
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

@register_command("/set_group_admin",help_text = "/set_group_admin <目标QQ号> -> 设置群管理员(admin)")
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

@register_command("/del_group_admin",help_text = "/del_group_admin <目标QQ号> -> 取消群管理员(admin)")
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

#将help命令放在最后
@register_command("/help","/h",help_text = "/help 或者 /h -> 查看帮助")
async def handle_help(msg, is_group=True):
    # 定义命令分类
    command_categories = {
        "1": {"name": "漫画相关", "commands": ["/jm", "/jmrank", "/search","/tag"]},
        "2": {"name": "收藏管理", "commands": ["/get_fav", "/add_fav", "/del_fav","/list_fav"]},
        "3": {"name": "聊天设置", "commands": ["/set_prompt", "/del_prompt", "/get_prompt","/del_message"]},
        "4": {"name": "娱乐功能", "commands": ["/random_image", "/random_emoticons", "/st","/random_video","/random_dice","/random_rps","/music"]},
        "5": {"name": "系统处理", "commands": ["/restart", "/tts", "/agree","/remind","/premind","/set_admin","/del_admin","/get_admin","/set_ids","/set_online_status","/get_friends","/set_qq_avatar","/send_like"]},
        "6": {"name": "群聊管理", "commands": ["/set_group_admin", "/del_group_admin"]}
    }

    # 第一阶段：显示分类菜单
    if not msg.raw_message.strip().endswith("help") and not msg.raw_message.strip().endswith("h"):
        # 用户选择了分类
        selected_category = msg.raw_message.split()[-1]
        if selected_category in command_categories:
            # 显示该分类下的详细命令
            help_text = f"{command_categories[selected_category]['name']}命令喵~\n"
            for cmd in command_categories[selected_category]['commands']:
                # 精确匹配命令别名
                for command_aliases, handler_func in command_handlers.items():
                    if cmd in command_aliases:
                        handler = handler_func
                        break
                if hasattr(handler, 'help_text'):
                    help_text += handler.help_text + "\n"
            
            if is_group:
                await msg.reply(text=help_text)
            else:
                await bot.api.post_private_msg(msg.user_id, text=help_text)
            return

    # 显示主帮助菜单
    help_text = "欢迎使用喵~ 请选择分类查看详细命令喵~\n"
    for num, category in command_categories.items():
        help_text += f"{num}. {category['name']}\n"
    
    help_text += "\n输入 /help 加分类编号查看详细命令，例如: /help 1"
    
    if is_group:
        await msg.reply(text=help_text)
    else:
        await bot.api.post_private_msg(msg.user_id, text=help_text)
