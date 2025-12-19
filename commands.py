from ncatbot.core import BotClient, GroupMessage, PrivateMessage
from config import load_config
from chat import group_messages, user_messages, tts, chat, generate_today_summary, summarize_group_text
import jmcomic,requests,random,configparser,json,yaml,re,os,asyncio
from jmcomic import *
from typing import Dict, List
from datetime import datetime
from difflib import get_close_matches  # 用于模糊匹配
from ncatbot.core import (
    MessageChain,  
    Text,         
    Reply,         
    At,           
    AtAll,         
    Dice,          
    Face,          
    Image,         
    Json,          
    Music,         
    CustomMusic,   
    Record,        
    Rps,          
    Video,         
    File,          
)
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

black_list_comic = {"global": [], "groups": {}, "users": {}} # str,黑名单

running = {}  #用于定时聊天的开关
tasks = {}  # 用于存储聊天的定时任务

books = {}

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

def load_address(): # 加载配置文件，返回图片保存地址
    with open("option.yml", "r", encoding="utf-8") as f:
        conf = yaml.safe_load(f)
        after_photo_list = conf.get("plugins", {}).get("after_album", [])
        if after_photo_list and isinstance(after_photo_list, list):
            pdf_dir = after_photo_list[0].get("kwargs", {}).get("pdf_dir", "./cache/pdf/")
        else:
            pdf_dir = "./cache/pdf/"
        pdf_dir = os.path.normpath(pdf_dir)
        return os.path.dirname(pdf_dir)  # 返回pdf目录的父目录

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
            print(f"任务 {name} 执行完成")
    else:
        await asyncio.sleep(delay_hours * 3600)  # 转换为秒
        await task_func(*args, **kwargs)
        print(f"任务 {name} 执行完成")
        del schedule_tasks[name]

async def chatter(id):
    """
    定时聊天函数。
    :param msg: 消息对象。
    """
    content = chat(content="现在请你根据上下文，主动和用户聊天",user_id=id)
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
            print(f"主动聊天循环出错: {e}")
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
        print(f"写入黑名单文件失败: {e}")

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
        print(f"写入定时聊天开关文件失败: {e}")

def load_running():
    """
    加载定时聊天开关
    """
    cache_dir = os.path.join(load_address(),"running/")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(os.path.join(cache_dir,"running.json"), "r", encoding="utf-8") as f:
            running.update(json.load(f))
    except FileNotFoundError:
        write_running()

def update_running(id):
    if id in tasks:
        tasks[id].cancel()  # 取消之前的任务
        tasks[id] = asyncio.create_task(chat_loop(id))  # 创建新的任务
        
def load_novel_data():
    """
    加载小说数据
    """
    with open("novel_details2.json", "r", encoding="utf-8") as f:
        books.update(json.load(f))

def fetch_cover_url(id:str) -> str:
    """
    获取指定本子的第一张图片URL
    :param album_id: 本子ID
    :return: 第一张图片的URL
    """
    """
    client = JmOption.default().new_jm_client()
    album = client.get_album_detail(id)
    first_photo = album[0]
    photo_detail = client.get_photo_detail(first_photo.photo_id, False)
    first_image = next(iter(photo_detail))
    return first_image.img_url
    """
    return f"https://cdn-msp3.jmapinodeudzn.net/media/photos/{id}/00001.webp"


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
switch = SwitchManager() #加载开关
switch.load_switches()
switch.add_switch('tts', default_value=False, description='TTS语音开关')
switch.add_switch('jm_send', default_value=True, description='漫画发送开关')
switch.add_switch('jm_send_user', default_value=False, description='用户私信发送漫画开关')
switch.add_switch('command', default_value=True, description='命令开关')
switch.add_switch('pdf_password', default_value=False, description='PDF密码开关')
switch.add_switch('summary_auto', default_value=False, description='每日自动总结开关')
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
    name = time.time()
    tot = 0
    fg=0
    comic_cache.clear()
    with open(os.path.join(cache_dir , f"{select}_{name}.md"), "w", encoding="utf-8") as f:
        f.write(f"## {select}：  \n")
    for page in cl.categories_filter_gen(page=1,  # 起始页码
                                         # 下面是分类参数
                                         time=JmMagicConstants.TIME_WEEK,
                                         category=JmMagicConstants.CATEGORY_ALL,
                                         order_by=JmMagicConstants.ORDER_BY_VIEW,
                                         ):
        for aid, atitle in page:
            tot += 1
            cover_url = fetch_cover_url(aid)
            with open(os.path.join(cache_dir , f"{select}_{name}.md"), "a", encoding="utf-8") as f:
                f.write(f"{tot}: {aid} {atitle}  \n ![]({cover_url})    \n\n")
            comic_cache.append(aid)
            if tot >=100:
                fg=1
                break
        if fg:
            break
    if not os.path.exists(os.path.join(cache_dir , f"{select}_{name}.md")):
        if is_group:
            await msg.reply(text="获取排行失败喵~，文件不存在")
        else:
            await bot.api.post_private_msg(msg.user_id, text="获取排行失败喵~，文件不存在")
        return
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=os.path.join(cache_dir , f"{select}_{name}.md"))
    else:
        await bot.api.upload_private_file(msg.user_id, os.path.join(cache_dir , f"{select}_{name}.md"), f"{select}_{name}.md")

@register_command("/search",help_text = "/search <内容> -> 搜索漫画",category = "1")
async def handle_search(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = os.path.join(load_address(),"search")
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
        with open(os.path.join(cache_dir , f"{id}.md"), "w", encoding="utf-8") as f:
            f.write(f"标题：{album.title}  \n标签：{album.tags}  \n页数：{album.page_count}  \n浏览次数：{album.views}  \n评论数：{album.comment_count}  \n ![](https://cdn-msp3.jmapinodeudzn.net/media/photos/{id}/00001.webp)")
        if is_group:
            await bot.api.post_group_file(msg.group_id, file=os.path.join(cache_dir , f"{id}.md"))
        else:
            await bot.api.upload_private_file(msg.user_id, os.path.join(cache_dir , f"{id}.md"), f"{id}.md")
        return

    name = content + str(time.time()).replace(".", "")
    
    with open(os.path.join(cache_dir , f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(f"搜索结果：{content}  \n")
    tot = 0
    for i in range(5):# 搜索5页，可以自己修改
        page: JmSearchPage = client.search_site(search_query=content, page=i+1,order_by=JmMagicConstants.ORDER_BY_VIEW)
        for album_id, title in page:
            tot += 1
            url = fetch_cover_url(album_id)
            with open(os.path.join(cache_dir , f"{name}.md"), "a", encoding="utf-8") as f:
                f.write(f"{tot}: {album_id}  {title}  \n![]({url})     \n\n")
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=os.path.join(cache_dir , f"{name}.md"))
    else:
        await bot.api.upload_private_file(msg.user_id, os.path.join(cache_dir , f"{name}.md"), f"{content}.md")

@register_command("/tag",help_text = "/tag <标签> -> 搜索漫画标签",category = "1")
async def handle_search(msg, is_group=True):
    if is_group:
        await msg.reply(text="正在搜索喵~")
    else:
        await bot.api.post_private_msg(msg.user_id, text="正在搜索喵~")

    cache_dir = os.path.join(load_address(),"search")
    os.makedirs(cache_dir,exist_ok = True)
    content = msg.raw_message[len("/tag"):].strip()
    name = content + str(time.time()).replace(".", "")
    client = JmOption.default().new_jm_client()
 
    with open(os.path.join(cache_dir , f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(f"搜索标签结果：{content}  \n")
    tot = 0
    for i in range(5):# 搜索5页，可以自己修改
        page: JmSearchPage = client.search_tag(search_query=content, page=i+1,order_by=JmMagicConstants.ORDER_BY_VIEW)
        for album_id, title in page:
            tot += 1
            url = fetch_cover_url(album_id)
            with open(os.path.join(cache_dir , f"{name}.md"), "a", encoding="utf-8") as f:
                f.write(f"{tot}: {album_id}  {title}  \n![]({url})    \n\n")
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=os.path.join(cache_dir , f"{name}.md"))
    else:
        await bot.api.upload_private_file(msg.user_id, os.path.join(cache_dir , f"{name}.md"), f"{content}.md")

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
        for aid, atitle in page.iter_id_title():
            url = fetch_cover_url(aid)
            with open(os.path.join(cache_dir , f"{name}.md"), "a", encoding="utf-8") as f:
                f.write(f"{aid}  {atitle}    \n![{atitle}]({url})    \n\n")
    if is_group:
        await bot.api.post_group_file(msg.group_id, file=os.path.join(cache_dir , f"{name}.md"))
    else:
        await bot.api.upload_private_file(msg.user_id, os.path.join(cache_dir , f"{name}.md"), f"{username}.md")

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

        if os.path.exists(os.path.join(load_address(),f"pdf/{comic_id}.pdf")):
            file_size = os.path.getsize(os.path.join(load_address(),f"pdf/{comic_id}.pdf"))
            if is_group:
                if switch.get_switch_state('jm_send', group_id=str(msg.group_id)):
                    if switch.get_switch_state('jm_send_user', group_id=str(msg.group_id)):
                        await bot.api.post_private_msg(msg.user_id,text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送喵~")
                        await bot.api.upload_private_file(msg.user_id, os.path.join(load_address(),f"pdf/{comic_id}.pdf"), f"{comic_id}.pdf")
                    else:
                        await msg.reply(text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送到群组喵~")
                        await bot.api.post_group_file(msg.group_id, file=os.path.join(load_address(),f"pdf/{comic_id}.pdf"))
                else:
                    await msg.reply(text=f"群组发送漫画已关闭喵~")
            else:
                if switch.get_switch_state('jm_send', user_id=str(msg.user_id)):
                    await bot.api.post_private_msg(msg.user_id,text=f"该漫画已存在喵~,文件大小：{file_size:.2f} MB，正在发送喵~")
                    await bot.api.upload_private_file(msg.user_id, os.path.join(load_address(),f"pdf/{comic_id}.pdf"), f"{comic_id}.pdf")
                else:
                    await msg.reply(text=f"该漫画已下载，但用户私信发送漫画已关闭喵~")
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
        except JmcomicException as e:
            error_msg = "当前禁漫站点接口不可用喵~ 可能是 /setting 接口返回异常，请稍后重试或检查jmcomic配置喵~"
            if is_group:
                await msg.reply(text=error_msg)
            else:
                await bot.api.post_private_msg(msg.user_id, text=error_msg)
            return
        try:
            album: JmAlbumDetail = client.get_album_detail(comic_id)
        except MissingAlbumPhotoException as e:
            error_msg = f"该漫画ID不存在喵~"
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
                jmcomic.create_option_by_file('./option.yml')
            )
        )

        file_path = os.path.join(load_address(), f"pdf/{comic_id}.pdf")

        # 检查文件是否真正生成
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"PDF文件未生成：{file_path}")
        
        #pdf加密功能，密码为comic_id
        if switch.get_switch_state('pdf_password', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None):
            try:
                import pikepdf
                with pikepdf.open(file_path,allow_overwriting_input=True) as pdf:

                    pdf.save(file_path, encryption=pikepdf.Encryption(
                        owner=comic_id,  # 所有者密码
                        user=comic_id,   # 用户密码
                        R=4              # 加密版本
                    ))
            except ImportError:
                error_msg = "缺少pikepdf库，无法加密PDF文件喵~"
                msg.reply(text=error_msg)

        if not switch.get_switch_state('jm_send', group_id=str(msg.group_id) if is_group else None,user_id=str(msg.user_id) if not is_group else None):
            await msg.reply(text="漫画已下载，但发送已关闭喵~")
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
        file_path = os.path.join(load_address(), f"pdf/{comic_id}.pdf")
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
        await msg.reply(text=reply)

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
    os.makedirs("prompts/group", exist_ok=True)
    os.makedirs("prompts/user", exist_ok=True)

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

@register_command("/get_prompt","/gp",help_text = "/get_prompt 或者 /gp -> 获取提示词(仅群admin)",category = "2")
async def handle_get_prompt(msg, is_group=True):
    if (str(msg.user_id) not in admin) and is_group:
        reply = "你没有权限喵~"
        await msg.reply(text=reply)
        return

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
    # 立即回复用户
    initial_text = "正在获取喵~"
    
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
        await msg.reply(text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")
    else:
        try:
            del user_messages[str(msg.user_id)]
        except KeyError:
            await bot.api.post_private_msg(msg.user_id, text="你没有对话记录喵~")
            return
        with open("saved_message/user_messages.json", "w", encoding="utf-8") as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        await bot.api.post_private_msg(msg.user_id, text="主人要离我而去了吗？呜呜呜……好吧，那我们以后再见喵~")

@register_command("/remind",help_text="/remind <时间(小时)> <内容> -> 定时提醒",category = "7")
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
            
    except ValueError as e:
        error_msg = "格式错误喵~ 使用: /precise_remind MM-DD HH:MM 提醒内容"
        if is_group:
            await msg.reply(text=error_msg)
        else:
            await bot.api.post_private_msg(msg.user_id, text=error_msg)

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
    except Exception as e:
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
    cache_dir = os.path.join(load_address(),"聊天记录.txt")
    if is_group:  
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
        with open("saved_message/user_messages.json","r",encoding="utf-8") as f:
            user_messages = json.load(f)
        try:
            text = str(user_messages[str(msg.user_id)])
        except KeyError:
            text = "你没有聊天记录喵~"
        with open(cache_dir,"w",encoding="utf-8") as f:
            f.write(text)
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

@register_command("/主动聊天",help_text = "/主动聊天 <间隔时间(小时)> <是否开启(1/0)> -> 开启主动聊天",category = "2")
async def handle_active_chat(msg, is_group=True):
    if is_group:
        await msg.reply(text="只能私聊设置喵~")
        return

    try:
        params = msg.raw_message[len("/主动聊天"):].strip().split()
        if len(params) < 2 or params[1] not in ('0', '1'):
            raise ValueError
            
        interval = float(params[0])
        active = bool(int(params[1]))
        user_id = str(msg.user_id)

        # 统一处理用户状态
        running[user_id] = {
            "interval": interval, 
            "active": active,
            "state": False
        }

        # 获取最近联系人
        try:
            ori = await bot.api.get_recent_contact(100)
            for contact in ori.get("data", []):
                if str(contact['lastestMsg'].get("user_id")) == user_id:
                    running[user_id]["last_time"] = contact['lastestMsg']["time"]
                    break
        except Exception as e:
            print(f"获取最近联系人失败: {e}")

        # 处理任务状态
        if active:
            if user_id in tasks:
                tasks[user_id].cancel()
            tasks[user_id] = asyncio.create_task(chat_loop(user_id))
        else:
            if user_id in tasks:
                tasks[user_id].cancel()
                del tasks[user_id]

        write_running()
        reply = f"设置成功喵~，{'现在'+str(interval)+'小时后会主动聊天喵~' if active else '已关闭主动聊天喵~'}"
        await bot.api.post_private_msg(user_id, text=reply)
        
    except ValueError:
        await bot.api.post_private_msg(msg.user_id, 
            text="格式错误喵~ 请输入 /主动聊天 间隔时间(小时) 是否开启(1/0)")

# 添加临时存储字典
temp_selections = {}
api_book = {}

@register_command("/findbook","/fb",help_text="/findbook 或者 /fb <书名> -> 搜索并选择下载轻小说",category = "6")
async def handle_find_book(msg, is_group=True):
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

    # 模糊匹配书籍
    matches = []
    for title, book_info in books.items():
        # 同时匹配原始书名和去除括号后的书名
        clean_title = re.sub(r'\(.*?\)', '', title).strip()
        if (search_term.lower() in title.lower() or search_term.lower() in clean_title.lower()):
            author = book_info.get("author")
            matches.append((author,title, book_info.get("download_url")))

    api_book[msg.user_id] = find_book_from_api(search_term)

    if not matches:
        matches2 = get_close_matches(search_term, books.keys(), n=5, cutoff=0.4)
        for title in matches2:
            author = books[title].get("author")
            matches.append((author,title, books[title].get("download_url")))

    if not matches and not api_book[msg.user_id]:
        reply = f"没有找到包含'{search_term}'的轻小说喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return
    
    # 生成选择列表
    choices = "\n".join([f"{i+1}. {title} -- {author}" for i, (author,title, _) in enumerate(matches)])
    if api_book[msg.user_id]:
        api_text = "\n".join([f"{i+1+len(matches)}. {title}" for i, (_, title) in enumerate(api_book[msg.user_id].items())])
        choices += f"\n\nAPI找到以下匹配的小说喵~:\n{api_text}"

    reply = f"找到以下匹配的轻小说喵~:\n{choices}\n\n请回复'/select 编号'选择要下载的轻小说喵~\n回复'/info 编号'获取轻小说信息喵~"
    
    
    # 存储匹配结果临时数据
    temp_selections[msg.user_id] = matches
    
    if is_group:
        await msg.reply(text=reply)
    else:
        await bot.api.post_private_msg(msg.user_id, text=reply)

@register_command("/fa",help_text="/fa <作者> -> 搜索作者",category = "6")
async def handle_find_author(msg, is_group=True):
    search_term = msg.raw_message[len("/fa"):].strip()
    if not search_term:
        reply = "请输入要搜索的作者喵~"
        if is_group:
            await msg.reply(text=reply)
        else:
            await bot.api.post_private_msg(msg.user_id, text=reply)
        return

    # 模糊匹配作者
    matches = []
    for title, book_info in books.items():
        author = book_info.get("author")
        if search_term.lower() in author.lower():
            matches.append((author, title, book_info.get("download_url")))

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

        
def find_book_from_api(search_term: str) -> list:
    """
    从API搜索小说
    :param search_term: 搜索关键词
    :return: 包含匹配小说信息的列表
    """
    url = "http://43.248.77.205:22222/api/search"
    params = {
        "key": search_term,
        "tab_type": 3,
    }
    res = requests.get(url, params=params)
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
    url = "http://43.248.77.205:22222/api/content"
    params = {
        "tab":"下载",
        "book_id":id
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        content = response.text
        # 确保目录存在
        path = os.path.join(os.path.dirname(__file__), f"cache/novel/{name}.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
    else:
        print(f"下载失败，状态码：{response.status_code}")

def get_api_book_info(id):
    url = "http://43.248.77.205:22222/api/detail"
    params = {
        "book_id":id
    }
    response = requests.get(url, params=params)
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
        print(f"获取失败，状态码：{response.status_code}")
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
        matches = temp_selections[msg.user_id]
        api_books = api_book[msg.user_id]

        if 0 <= selection < len(matches) or (selection >= len(matches) and selection < len(matches) + len(api_books)):
            if selection < len(matches):
                author,title, url = matches[selection]
                reply = f"已开始下载《{title}》-- {author}喵~"
                if is_group:
                    await msg.reply(text=reply)
                    await bot.api.post_group_file(msg.group_id,file=url)
                else:
                    await bot.api.post_private_msg(msg.user_id, text=reply)
                    await bot.api.upload_private_file(msg.user_id,file=url,name=title+".txt")     
            else:
                id, title = list(api_books.items())[selection - len(matches)]
                download_api_book(id,title)
                reply = f"已开始下载《{title}》喵~"
                await msg.reply(text=reply)
                api_book_file_path = os.path.join(os.path.dirname(__file__), f"cache/novel/{title}.txt")
                if is_group:
                    await bot.api.post_group_file(msg.group_id,file=api_book_file_path)
                else:
                    await bot.api.upload_private_file(msg.user_id,file=api_book_file_path,name=title+".txt")
            
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
    
    del temp_selections[str(msg.user_id)] 
    del api_books[str(msg.user_id)]  

@register_command("/info",help_text="/info <书名> -> 获取轻小说信息",category = "6")
async def handle_info(msg, is_group=True):
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
                info = books[title]
                try:
                    introduction = info['introduction']
                except Exception:
                    introduction = "暂无"
                cover = info['cover_url']
                reply =  MessageChain(
                f"《{title}》的信息如下喵~\n作者: {author}\n分类: {info['category']}\n字数: {info['word_count']}\n状态: {info['is_serialize']}\n热度：{info['hot']}\n简介：{introduction}\n更新日期: {info['last_date']}\n下载链接: {url}\n详细页面：{info['page']}",Image(f"{cover}")
                )
                if is_group:
                    await msg.reply(rtf=reply)
                else:
                    await bot.api.post_private_msg(msg.user_id, rtf=reply)
            else:
                id, title = list(api_books.items())[selection - len(matches)]
                
                info = get_api_book_info(id)
                if(info==None):
                    reply = "没有找到该轻小说的信息喵~"
                    await msg.reply(text=reply)
                    return

                reply =  MessageChain(
                f"《{title}》的信息如下喵~\n作者: {info['author']}\n分类: {info['category']}\n字数: {info['word_count']}\n状态: {info['is_serialize']}\n热度：{info['hot']}\n简介：{info['introduction']}\n更新日期: {info['last_date']}\n下载链接: {info['download_url']}\n详细页面：{info['page']}",Image(f"{info['cover']}")

                )
                await msg.reply(rtf=reply)

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

@register_command("/random_novel","/rn",help_text = "/random_novel 或者 /rn -> 发送随机小说",category = "6")
async def handle_random_novel(msg, is_group=True):
    novel = random.choice(list(books.keys()))
    url = books[novel]["download_url"]
    hot = books[novel].get('hot', '未知')
    reply = f"抽选到了《{novel}》喵~\n"
    reply += f"简介如下喵~\n作者：{books[novel]['author']}\n字数：{books[novel]['word_count']}\n状态：{books[novel]['is_serialize']}\n热度：{hot}\n最新更新：{books[novel]['last_date']}\n简介：{books[novel]['introduction']}\n下载链接：{url}"
    cover = books[novel]['cover_url']
    reply =  MessageChain(
        reply,
        Image(f"{cover}")
        )
    if is_group:
        await msg.reply(rtf=reply)
        await bot.api.post_group_file(msg.group_id, file=url)
    else:
        await bot.api.post_private_msg(msg.user_id, rtf=reply)
        await bot.api.upload_private_file(msg.user_id, file=url,name=novel+".txt")

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
    except Exception as e:
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
    except Exception as e:
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
                    help_text += f"你没有权限查看当前分类的命令喵~\n"

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
