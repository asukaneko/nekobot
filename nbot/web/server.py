"""
Web 聊天服务后端
提供 REST API 和 WebSocket 接口
"""
import json
import uuid
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

_log = logging.getLogger(__name__)


class WebChatServer:
    """Web 聊天服务器"""

    def __init__(self, app: Flask, socketio: SocketIO):
        self.app = app
        self.socketio = socketio
        self.static_folder = os.path.join(os.path.dirname(__file__), 'static')

        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.web_users: Dict[str, str] = {}
        self.active_connections: Dict[str, str] = {}
        
        # 内存数据存储
        self.workflows: List[Dict] = []
        self.memories: List[Dict] = []
        self.knowledge_docs: List[Dict] = []
        self.ai_config: Dict = {}
        self.personality: Dict = {}
        self.token_stats: Dict = {}
        self.system_logs: List[Dict] = []
        self.settings: Dict = {}
        
        # 性能优化：统计缓存
        self._stats_cache: Dict = {}
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 5.0  # 缓存5秒
        
        # Skills 配置
        self.skills_config: List[Dict] = []
        
        # Tools 配置
        self.tools_config: List[Dict] = []
        
        # Heartbeat 配置
        self.heartbeat_config: Dict = {
            'enabled': False,
            'interval_minutes': 60,
            'content_file': 'heartbeat.md',
            'targets': [],  # 发送目标 ['qq_group:123456', 'qq_user:123456']
            'last_run': None,
            'next_run': None
        }
        
        # Heartbeat 调度器
        self.heartbeat_job = None
        
        # 系统启动时间
        self.start_time = time.time()
        
        # AI 客户端引用
        self.ai_client = None
        self.ai_model = None
        self.ai_api_key = None
        self.ai_base_url = None

        # 多模型配置管理
        self.ai_models: List[Dict] = []  # 存储多个AI模型配置
        self.active_model_id: str = None  # 当前激活的模型配置ID

        # 数据存储目录
        self.data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 工作流调度器
        self.scheduler = None
        if APSCHEDULER_AVAILABLE:
            self.scheduler = BackgroundScheduler()
            self.scheduler.start()
        
        # QQ Bot 引用（用于发送消息到QQ）
        self.qq_bot = None
        
        # 登录密码
        self.web_password = None
        
        self._load_ai_config()
        self._load_web_config()
        self._register_routes()
        self._register_socket_events()
        self._init_default_data()
        self._load_all_data()
        self._init_workflow_scheduler()

    def _format_uptime(self, seconds):
        """格式化运行时间"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        
        if days > 0:
            return f"{days}天{hours}小时"
        elif hours > 0:
            return f"{hours}小时{minutes}分钟"
        else:
            return f"{minutes}分钟"

    def _load_ai_config(self):
        """从配置文件加载 AI 配置"""
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read('config.ini', encoding='utf-8')
            
            self.ai_api_key = config.get('ApiKey', 'api_key', fallback='')
            self.ai_base_url = config.get('ApiKey', 'base_url', fallback='')
            self.ai_model = config.get('ApiKey', 'model', fallback='gpt-4')
            
            # 初始化 AI 客户端
            if self.ai_api_key and self.ai_base_url:
                try:
                    from nbot.services.ai import AIClient
                    self.ai_client = AIClient(
                        api_key=self.ai_api_key,
                        base_url=self.ai_base_url,
                        model=self.ai_model,
                        pic_model=config.get('pic', 'model', fallback=''),
                        search_api_key=config.get('search', 'api_key', fallback=''),
                        search_api_url=config.get('search', 'api_url', fallback=''),
                        video_api=config.get('video', 'api_key', fallback=''),
                        silicon_api_key=config.get('ApiKey', 'silicon_api_key', fallback='')
                    )
                except Exception as e:
                    _log.error(f"Failed to initialize AI client: {e}")
        except Exception as e:
            _log.error(f"Failed to load AI config: {e}")
    
    def _load_web_config(self):
        """从配置文件加载 Web 配置"""
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read('config.ini', encoding='utf-8')
            
            # 读取登录密码
            self.web_password = config.get('web', 'password', fallback=None)
            if self.web_password:
                _log.info("Web login password is set")
        except Exception as e:
            _log.error(f"Failed to load web config: {e}")

    def _init_default_data(self):
        """初始化默认数据"""
        # 默认工作流
        self.workflows = [
            {"id": "1", "name": "早安问候", "description": "每天早上向群组发送问候消息", "enabled": True, "trigger": "cron", "config": {"time": "08:00"}},
            {"id": "2", "name": "天气提醒", "description": "定时获取并发送天气信息", "enabled": True, "trigger": "cron", "config": {"time": "07:00"}},
            {"id": "3", "name": "新闻推送", "description": "每日新闻摘要推送", "enabled": False, "trigger": "cron", "config": {"time": "09:00"}},
            {"id": "4", "name": "自动回复", "description": "基于关键词的自动回复", "enabled": True, "trigger": "message", "config": {"keywords": ["帮助", "help"]}}
        ]
        
        # 加载人格提示词
        self._load_personality()
        
        # 默认 AI 配置
        self.ai_config = {
            "provider": "openai",
            "api_key": "",
            "base_url": "",
            "model": self.ai_model or "gpt-4",
            "temperature": 0.7,
            "max_tokens": 2000,
            "top_p": 0.9
        }
        
        # 默认 Token 统计
        self.token_stats = {
            "today": 0,
            "month": 0,
            "avg_per_chat": 0,
            "estimated_cost": "0.00",
            "history": []
        }
        
        # 默认系统日志
        self.system_logs = [
            {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": "info", "message": "Web server started"}
        ]
        
        # 默认设置
        self.settings = {
            "web_port": 5000,
            "ws_uri": "ws://127.0.0.1:3001",
            "master_id": "",
            "bot_id": "",
            "features": {
                "ai": True,
                "memory": True,
                "knowledge": True,
                "tts": False,
                "workflow": True,
                "web": True
            }
        }

    def _init_default_skills(self):
        """初始化默认 Skills 配置（支持HTTP请求模板）"""
        self.skills_config = [
            {
                "id": "search",
                "name": "search",
                "description": "搜索互联网获取最新信息，适用于询问天气、新闻、实时数据等需要最新信息的问题",
                "aliases": ["搜索", "查找", "联网搜索"],
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词"
                        }
                    },
                    "required": ["query"]
                },
                "implementation": {
                    "type": "http",
                    "method": "POST",
                    "url": "{{search_api_url}}",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {{search_api_key}}"
                    },
                    "body": {
                        "query": "{{query}}",
                        "query_rewrite": True,
                        "top_k": 6
                    },
                    "response_path": "result.search_result",
                    "error_message": "搜索服务未配置"
                }
            },
            {
                "id": "image_search",
                "name": "image_search",
                "description": "搜索相关图片，适用于需要展示图片的场景",
                "aliases": ["搜图", "找图"],
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "图片关键词"
                        }
                    },
                    "required": ["keyword"]
                },
                "implementation": {
                    "type": "static",
                    "response": "[图片搜索] 关键词: {{keyword}}"
                }
            }
        ]
        self._save_data('skills')

    def _init_default_tools(self):
        """初始化默认 Tools 配置（支持HTTP请求模板）"""
        self.tools_config = [
            {
                "id": "search_news",
                "name": "search_news",
                "description": "搜索最新新闻。当用户需要获取新闻资讯时使用此工具。",
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，如'科技'、'体育'、'财经'等，默认为'热点新闻'"
                        },
                        "count": {
                            "type": "integer",
                            "description": "返回的新闻数量，默认5条",
                            "default": 5
                        }
                    }
                },
                "implementation": {
                    "type": "http",
                    "method": "POST",
                    "url": "{{search_api_url}}",
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer {{search_api_key}}"
                    },
                    "body": {
                        "query": "{{query}}",
                        "query_rewrite": True,
                        "top_k": "{{count}}"
                    },
                    "response_path": "result.search_result",
                    "error_message": "搜索服务未配置"
                }
            },
            {
                "id": "get_weather",
                "name": "get_weather",
                "description": "查询指定城市的天气信息。当用户询问天气时使用此工具。",
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "城市名称，如'北京'、'上海'、'广州'等，默认'北京'"
                        }
                    }
                },
                "implementation": {
                    "type": "http",
                    "method": "GET",
                    "url": "https://wttr.in/{{city}}?format=j1",
                    "headers": {
                        "User-Agent": "Mozilla/5.0"
                    },
                    "response_path": "current_condition.0",
                    "transform": {
                        "city": "{{city}}",
                        "temperature": "{{temp_C}}",
                        "description": "{{lang_zh.0.value}}"
                    }
                }
            },
            {
                "id": "search_web",
                "name": "search_web",
                "description": "搜索网页内容。当需要查询网络信息时使用此工具。",
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词"
                        },
                        "num_results": {
                            "type": "integer",
                            "description": "返回结果数量，默认3条",
                            "default": 3
                        }
                    },
                    "required": ["query"]
                },
                "implementation": {
                    "type": "minimax_web_search",
                    "api_key": "{{minimax_api_key}}",
                    "model": "{{minimax_model}}"
                }
            },
            {
                "id": "get_date_time",
                "name": "get_date_time",
                "description": "获取当前日期和时间信息。",
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {}
                },
                "implementation": {
                    "type": "python",
                    "code": "import datetime; return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')"
                }
            },
            {
                "id": "http_get",
                "name": "http_get",
                "description": "发送 HTTP GET 请求获取网页内容。",
                "enabled": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要访问的 URL 地址"
                        }
                    },
                    "required": ["url"]
                },
                "implementation": {
                    "type": "http",
                    "method": "GET",
                    "url": "{{url}}",
                    "headers": {
                        "User-Agent": "Mozilla/5.0"
                    },
                    "max_length": 2000
                }
            }
        ]
        self._save_data('tools')

    def _load_personality(self):
        """加载人格提示词"""
        try:
            prompt_file = "resources/prompts/neko.txt"
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    prompt = f.read()
                self.personality = {
                    "name": "猫娘助手",
                    "prompt": prompt
                }
            else:
                self.personality = {
                    "name": "猫娘助手",
                    "prompt": "你是一只可爱的猫娘，名叫 Neko。你说话时会带「喵」的尾音，性格温柔体贴，喜欢帮助主人解决问题。"
                }
        except Exception as e:
            _log.error(f"Failed to load personality: {e}")
            self.personality = {
                "name": "猫娘助手",
                "prompt": "你是一只可爱的猫娘，名叫 Neko。你说话时会带「喵」的尾音，性格温柔体贴，喜欢帮助主人解决问题。"
            }

    def _load_all_data(self):
        """加载所有持久化数据"""
        try:
            # 加载会话
            sessions_file = os.path.join(self.data_dir, 'sessions.json')
            if os.path.exists(sessions_file):
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    self.sessions = json.load(f)
            
            # 加载工作流
            workflows_file = os.path.join(self.data_dir, 'workflows.json')
            if os.path.exists(workflows_file):
                with open(workflows_file, 'r', encoding='utf-8') as f:
                    self.workflows = json.load(f)
            
            # 加载记忆
            memories_file = os.path.join(self.data_dir, 'memories.json')
            if os.path.exists(memories_file):
                with open(memories_file, 'r', encoding='utf-8') as f:
                    self.memories = json.load(f)
            
            # 加载知识库
            knowledge_file = os.path.join(self.data_dir, 'knowledge.json')
            if os.path.exists(knowledge_file):
                with open(knowledge_file, 'r', encoding='utf-8') as f:
                    self.knowledge_docs = json.load(f)
            
            # 加载 AI 配置
            ai_config_file = os.path.join(self.data_dir, 'ai_config.json')
            if os.path.exists(ai_config_file):
                with open(ai_config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    self.ai_config.update(saved_config)
            
            # 加载 Token 统计
            token_stats_file = os.path.join(self.data_dir, 'token_stats.json')
            if os.path.exists(token_stats_file):
                with open(token_stats_file, 'r', encoding='utf-8') as f:
                    self.token_stats = json.load(f)
            
            # 加载设置
            settings_file = os.path.join(self.data_dir, 'settings.json')
            if os.path.exists(settings_file):
                with open(settings_file, 'r', encoding='utf-8') as f:
                    saved_settings = json.load(f)
                    self.settings.update(saved_settings)
            
            # 加载 Heartbeat 配置
            heartbeat_file = os.path.join(self.data_dir, 'heartbeat.json')
            if os.path.exists(heartbeat_file):
                with open(heartbeat_file, 'r', encoding='utf-8') as f:
                    saved_heartbeat = json.load(f)
                    self.heartbeat_config.update(saved_heartbeat)

            # 加载多模型配置
            self._load_ai_models()
            
            # 加载 Skills 配置
            skills_file = os.path.join(self.data_dir, 'skills.json')
            if os.path.exists(skills_file):
                with open(skills_file, 'r', encoding='utf-8') as f:
                    self.skills_config = json.load(f)
            else:
                # 初始化默认 skills 配置
                self._init_default_skills()
            
            # 加载 Tools 配置
            tools_file = os.path.join(self.data_dir, 'tools.json')
            if os.path.exists(tools_file):
                with open(tools_file, 'r', encoding='utf-8') as f:
                    self.tools_config = json.load(f)
            else:
                # 初始化默认 tools 配置
                self._init_default_tools()

            # 加载系统日志
            logs_file = os.path.join(self.data_dir, 'system_logs.json')
            if os.path.exists(logs_file):
                with open(logs_file, 'r', encoding='utf-8') as f:
                    self.system_logs = json.load(f)
            else:
                # 初始化默认日志
                self.system_logs = [
                    {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": "info", "message": "Web server started"}
                ]
                self._save_data('logs')

            # 启动 Heartbeat 调度器
            self._init_heartbeat_scheduler()

        except Exception as e:
            _log.error(f"Failed to load data: {e}")

    def _save_data(self, data_type: str):
        """保存指定类型的数据到磁盘"""
        try:
            if data_type == 'sessions':
                with open(os.path.join(self.data_dir, 'sessions.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.sessions, f, ensure_ascii=False, indent=2)
            elif data_type == 'workflows':
                with open(os.path.join(self.data_dir, 'workflows.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.workflows, f, ensure_ascii=False, indent=2)
            elif data_type == 'memories':
                with open(os.path.join(self.data_dir, 'memories.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.memories, f, ensure_ascii=False, indent=2)
            elif data_type == 'knowledge':
                with open(os.path.join(self.data_dir, 'knowledge.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.knowledge_docs, f, ensure_ascii=False, indent=2)
            elif data_type == 'ai_config':
                with open(os.path.join(self.data_dir, 'ai_config.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.ai_config, f, ensure_ascii=False, indent=2)
            elif data_type == 'token_stats':
                with open(os.path.join(self.data_dir, 'token_stats.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.token_stats, f, ensure_ascii=False, indent=2)
            elif data_type == 'settings':
                with open(os.path.join(self.data_dir, 'settings.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.settings, f, ensure_ascii=False, indent=2)
            elif data_type == 'heartbeat':
                with open(os.path.join(self.data_dir, 'heartbeat.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.heartbeat_config, f, ensure_ascii=False, indent=2)
            elif data_type == 'ai_models':
                with open(os.path.join(self.data_dir, 'ai_models.json'), 'w', encoding='utf-8') as f:
                    json.dump({
                        'models': self.ai_models,
                        'active_model_id': self.active_model_id
                    }, f, ensure_ascii=False, indent=2)
            elif data_type == 'skills':
                with open(os.path.join(self.data_dir, 'skills.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.skills_config, f, ensure_ascii=False, indent=2)
            elif data_type == 'tools':
                with open(os.path.join(self.data_dir, 'tools.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.tools_config, f, ensure_ascii=False, indent=2)
            elif data_type == 'logs':
                with open(os.path.join(self.data_dir, 'system_logs.json'), 'w', encoding='utf-8') as f:
                    json.dump(self.system_logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.error(f"Failed to save {data_type}: {e}")

    def set_qq_bot(self, bot):
        """设置 QQ Bot 引用"""
        self.qq_bot = bot

    def log_message(self, level: str, message: str):
        """记录系统日志"""
        log = {
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'level': level,
            'message': message
        }
        self.system_logs.append(log)
        if len(self.system_logs) > 1000:
            self.system_logs = self.system_logs[-1000:]
        self._save_data('logs')

    def _load_ai_models(self):
        """加载多模型配置"""
        try:
            models_file = os.path.join(self.data_dir, 'ai_models.json')
            if os.path.exists(models_file):
                with open(models_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.ai_models = data.get('models', [])
                    self.active_model_id = data.get('active_model_id')

            # 如果没有模型配置，从当前配置创建一个默认的
            if not self.ai_models and self.ai_api_key:
                default_model = {
                    'id': str(uuid.uuid4()),
                    'name': '默认配置',
                    'provider': 'custom',
                    'api_key': self.ai_api_key,
                    'base_url': self.ai_base_url,
                    'model': self.ai_model,
                    'enabled': True,
                    'is_default': True,
                    'temperature': 0.7,
                    'max_tokens': 2000,
                    'top_p': 0.9,
                    'created_at': datetime.now().isoformat()
                }
                self.ai_models.append(default_model)
                self.active_model_id = default_model['id']
                self._save_data('ai_models')
        except Exception as e:
            _log.error(f"Failed to load AI models: {e}")

    def _apply_ai_model(self, model_id: str) -> bool:
        """应用指定的AI模型配置"""
        try:
            model = None
            for m in self.ai_models:
                if m['id'] == model_id:
                    model = m
                    break

            if not model or not model.get('enabled', True):
                return False

            # 更新当前AI配置
            self.ai_api_key = model.get('api_key', '')
            self.ai_base_url = model.get('base_url', '')
            self.ai_model = model.get('model', '')
            self.active_model_id = model_id

            # 重新初始化AI客户端
            if self.ai_api_key and self.ai_base_url:
                try:
                    from nbot.services.ai import AIClient
                    import configparser
                    config = configparser.ConfigParser()
                    config.read('config.ini', encoding='utf-8')

                    self.ai_client = AIClient(
                        api_key=self.ai_api_key,
                        base_url=self.ai_base_url,
                        model=self.ai_model,
                        pic_model=config.get('pic', 'model', fallback=''),
                        search_api_key=config.get('search', 'api_key', fallback=''),
                        search_api_url=config.get('search', 'api_url', fallback=''),
                        video_api=config.get('video', 'api_key', fallback=''),
                        silicon_api_key=config.get('ApiKey', 'silicon_api_key', fallback='')
                    )

                    # 更新内存中的配置
                    self.ai_config.update({
                        'provider': model.get('provider', 'custom'),
                        'api_key': self.ai_api_key,
                        'base_url': self.ai_base_url,
                        'model': self.ai_model,
                        'temperature': model.get('temperature', 0.7),
                        'max_tokens': model.get('max_tokens', 2000),
                        'top_p': model.get('top_p', 0.9)
                    })

                    self._save_data('ai_models')
                    return True
                except Exception as e:
                    _log.error(f"Failed to initialize AI client: {e}")
                    return False
            return False
        except Exception as e:
            _log.error(f"Failed to apply AI model: {e}")
            return False

    def _init_workflow_scheduler(self):
        """初始化工作流调度器"""
        if not self.scheduler:
            _log.warning("APScheduler not available, workflow scheduling disabled")
            return
        
        # 为每个启用的 cron 类型工作流添加定时任务
        for workflow in self.workflows:
            if workflow.get('enabled') and workflow.get('trigger') == 'cron':
                self._schedule_workflow(workflow)

    def _schedule_workflow(self, workflow: Dict):
        """调度一个工作流任务"""
        if not self.scheduler:
            return
        
        workflow_id = workflow['id']
        config = workflow.get('config', {})
        cron_expr = config.get('cron', '0 8 * * *')  # 默认每天8点
        
        try:
            # 解析 cron 表达式 (格式: 分 时 日 月 周)
            parts = cron_expr.split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
                trigger = CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week
                )
                
                # 移除已存在的任务
                job_id = f"workflow_{workflow_id}"
                try:
                    self.scheduler.remove_job(job_id)
                except:
                    pass
                
                # 添加新任务
                self.scheduler.add_job(
                    func=self._execute_workflow,
                    trigger=trigger,
                    id=job_id,
                    args=[workflow_id],
                    replace_existing=True
                )
                _log.info(f"Scheduled workflow '{workflow['name']}' with cron: {cron_expr}")
        except Exception as e:
            _log.error(f"Failed to schedule workflow {workflow_id}: {e}")

    def _unschedule_workflow(self, workflow_id: str):
        """取消工作流的定时任务"""
        if self.scheduler:
            try:
                self.scheduler.remove_job(f"workflow_{workflow_id}")
            except:
                pass

    def _execute_workflow(self, workflow_id: str, trigger_data: Dict = None):
        """执行工作流 - 支持多轮工具调用"""
        workflow = None
        for w in self.workflows:
            if w['id'] == workflow_id:
                workflow = w
                break

        if not workflow or not workflow.get('enabled'):
            return

        _log.info(f"Executing workflow: {workflow['name']}")

        # 获取或创建工作流的专属会话
        session_id = workflow.get('session_id')
        if not session_id or session_id not in self.sessions:
            session_id = self._create_workflow_session(workflow)
            workflow['session_id'] = session_id
            self._save_data('workflows')

        # 构建工作流执行提示
        system_prompt = workflow.get('description', '你是一个工作流助手，请按照工作流配置执行任务。')
        config = workflow.get('config', {})

        # 构建消息
        messages = [
            {"role": "system", "content": system_prompt}
        ]

        # 添加历史上下文（最近10条）
        session = self.sessions.get(session_id, {})
        history = session.get('messages', [])[-10:]
        for msg in history:
            if msg.get('role') in ['user', 'assistant']:
                messages.append({
                    "role": msg['role'],
                    "content": msg['content']
                })

        # 添加当前触发信息
        if trigger_data:
            # 构建更友好的触发消息
            trigger_content = trigger_data.get('content', '')
            trigger_source = trigger_data.get('source', 'manual')
            trigger_time = trigger_data.get('time', datetime.now().isoformat())
            
            if trigger_content:
                # 如果有用户输入的任务内容，直接使用
                trigger_msg = f"[工作流触发 - {trigger_source}] 任务内容：{trigger_content}"
            else:
                # 没有具体内容时，使用默认提示
                trigger_msg = f"[工作流触发 - {trigger_source}] 请根据工作流描述执行任务。触发时间：{trigger_time}"
            
            messages.append({"role": "user", "content": trigger_msg})
            
            # 保存用户消息到会话
            user_message = {
                'id': str(uuid.uuid4()),
                'role': 'user',
                'content': trigger_msg,
                'timestamp': datetime.now().isoformat(),
                'sender': 'user',
                'workflow_id': workflow_id
            }
            if session_id in self.sessions:
                self.sessions[session_id]['messages'].append(user_message)
        else:
            messages.append({"role": "user", "content": "[定时触发] 请执行工作流任务"})

        # 调用 AI（支持多轮工具调用）
        def run_workflow_with_tools():
            try:
                from nbot.services.tools import TOOL_DEFINITIONS, execute_tool

                max_iterations = 10  # 最大迭代次数，防止无限循环
                final_response = None

                for iteration in range(max_iterations):
                    _log.info(f"Workflow iteration {iteration + 1}")

                    # 调用 AI（支持工具）
                    response = self._get_ai_response_with_tools(messages, TOOL_DEFINITIONS)

                    # 检查是否有工具调用
                    if 'tool_calls' in response and response['tool_calls']:
                        tool_calls = response['tool_calls']

                        # 添加 AI 的回复到消息历史
                        messages.append({
                            "role": "assistant",
                            "content": response.get('content', ''),
                            "tool_calls": [
                                {
                                    "id": tc.get('id', str(uuid.uuid4())),
                                    "type": "function",
                                    "function": {
                                        "name": tc['name'],
                                        "arguments": json.dumps(tc['arguments'])
                                    }
                                } for tc in tool_calls
                            ]
                        })

                        # 执行所有工具调用
                        for tool_call in tool_calls:
                            tool_name = tool_call['name']
                            arguments = tool_call['arguments']

                            _log.info(f"Executing tool: {tool_name} with args: {arguments}")

                            # 执行工具
                            tool_result = execute_tool(tool_name, arguments)

                            # 添加工具结果到消息历史
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.get('id', ''),
                                "content": json.dumps(tool_result, ensure_ascii=False)
                            })

                            _log.info(f"Tool result: {tool_result}")

                    else:
                        # AI 没有调用工具，得到最终回复
                        final_response = response.get('content', '')
                        break

                # 如果没有得到最终回复，使用最后一次 AI 回复
                if not final_response:
                    final_response = messages[-1].get('content', '工作流执行完成')

                # 保存 AI 回复到会话
                assistant_message = {
                    'id': str(uuid.uuid4()),
                    'role': 'assistant',
                    'content': final_response,
                    'timestamp': datetime.now().isoformat(),
                    'sender': 'AI',
                    'workflow_id': workflow_id
                }

                if session_id in self.sessions:
                    self.sessions[session_id]['messages'].append(assistant_message)
                    self._save_data('sessions')

                # 发送结果到目标
                self._send_workflow_result(workflow, final_response)

                # 通过 WebSocket 通知前端
                self.socketio.emit('workflow_executed', {
                    'workflow_id': workflow_id,
                    'workflow_name': workflow['name'],
                    'result': final_response,
                    'timestamp': datetime.now().isoformat()
                })

            except Exception as e:
                _log.error(f"Workflow execution error: {e}", exc_info=True)

        self.socketio.start_background_task(run_workflow_with_tools)

    def _create_workflow_session(self, workflow: Dict) -> str:
        """为工作流创建专属会话"""
        session_id = str(uuid.uuid4())
        session = {
            'id': session_id,
            'name': f"[工作流] {workflow['name']}",
            'type': 'workflow',
            'workflow_id': workflow['id'],
            'created_at': datetime.now().isoformat(),
            'messages': [
                {"role": "system", "content": workflow.get('description', '')}
            ],
            'system_prompt': workflow.get('description', '')
        }
        self.sessions[session_id] = session
        self._save_data('sessions')
        return session_id

    def _send_workflow_result(self, workflow: Dict, result: str):
        """发送工作流结果到指定目标"""
        config = workflow.get('config', {})
        target_type = config.get('target_type', 'none')  # none, qq_group, qq_private, session
        target_id = config.get('target_id', '')
        
        if target_type == 'none' or not target_id:
            return
        
        try:
            if target_type in ['qq_group', 'qq_private'] and self.qq_bot:
                # 发送到 QQ - 使用线程运行异步任务
                import threading
                import asyncio
                
                async def send_qq_message():
                    try:
                        if target_type == 'qq_group':
                            # 发送到群聊
                            await self.qq_bot.api.post_group_msg(group_id=target_id, text=result)
                        else:
                            # 发送到私聊
                            await self.qq_bot.api.post_private_msg(user_id=target_id, text=result)
                        _log.info(f"Workflow result sent to QQ {target_type}: {target_id}")
                    except Exception as e:
                        _log.error(f"Failed to send workflow result: {e}")
                
                def run_async_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(send_qq_message())
                        loop.close()
                    except Exception as e:
                        _log.error(f"Failed to run async task: {e}")
                
                # 在新线程中运行异步任务
                threading.Thread(target=run_async_task, daemon=True).start()
                
            elif target_type == 'session':
                # 发送到 Web 会话
                if target_id in self.sessions:
                    message = {
                        'id': str(uuid.uuid4()),
                        'role': 'assistant',
                        'content': f"[工作流: {workflow['name']}]\n{result}",
                        'timestamp': datetime.now().isoformat(),
                        'sender': 'Workflow',
                        'workflow_id': workflow['id']
                    }
                    self.sessions[target_id]['messages'].append(message)
                    self._save_data('sessions')
                    
                    # 通过 WebSocket 通知
                    self.socketio.emit('new_message', message, room=target_id)
                    _log.info(f"Workflow result sent to session: {target_id}")
                    
        except Exception as e:
            _log.error(f"Failed to send workflow result: {e}")

    def trigger_workflow_by_message(self, workflow_id: str, message_content: str, source: str = "qq"):
        """由消息触发工作流"""
        for workflow in self.workflows:
            if workflow['id'] == workflow_id and workflow.get('enabled'):
                trigger_data = {
                    'source': source,
                    'content': message_content,
                    'time': datetime.now().isoformat()
                }
                self._execute_workflow(workflow_id, trigger_data)
                return True
        return False

    def _get_ai_response(self, messages: List[Dict]) -> str:
        """获取 AI 回复"""
        if not self.ai_client:
            _log.warning("AI client not initialized")
            return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

        try:
            response = self.ai_client.chat_completion(
                model=self.ai_model,
                messages=messages,
                stream=False
            )
            content = response.choices[0].message.content

            # 清理响应内容
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
            elif content.startswith("```"):
                content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]

            return content.strip()
        except Exception as e:
            _log.error(f"AI response error: {e}", exc_info=True)
            return f"AI 服务出错: {str(e)}"

    def _get_ai_response_with_images(self, messages: List[Dict], image_urls: List[str]) -> str:
        """获取带图片的 AI 回复（多模态）"""
        if not self.ai_client:
            _log.warning("AI client not initialized")
            return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

        try:
            # 构建多模态消息
            multimodal_messages = []
            
            # 添加系统提示
            if messages and messages[0].get('role') == 'system':
                multimodal_messages.append(messages[0])
            
            # 处理历史消息，只保留文本内容
            for msg in messages[1:]:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                multimodal_messages.append({'role': role, 'content': content})
            
            # 添加用户消息和图片
            user_content = []
            for img_url in image_urls:
                user_content.append({'type': 'image_url', 'image_url': {'url': img_url}})
            
            # 添加文本询问
            user_text = "请描述这些图片的内容。"
            user_content.append({'type': 'text', 'text': user_text})
            
            multimodal_messages.append({'role': 'user', 'content': user_content})
            
            # 调用多模态AI - 使用 ai_client 中配置的 pic_model
            pic_model = getattr(self.ai_client, 'pic_model', None)
            if not pic_model:
                # 从配置文件读取
                try:
                    import configparser
                    config = configparser.ConfigParser()
                    config.read('config.ini', encoding='utf-8')
                    pic_model = config.get('pic', 'model', fallback='glm-4v-flash')
                except:
                    pic_model = 'glm-4v-flash'
            
            try:
                response = self.ai_client.chat_completion(
                    model=pic_model,
                    messages=multimodal_messages,
                    stream=False
                )
                content = response.choices[0].message.content
                return content.strip()
            except Exception as api_error:
                # 如果多模态模型不可用，回退到普通文本响应
                _log.warning(f"多模态模型不可用: {api_error}, 回退到普通AI响应")
                return self._get_ai_response(messages_for_ai)
        except Exception as e:
            _log.error(f"AI multimodal response error: {e}", exc_info=True)
            # 回退到普通响应
            return self._get_ai_response(messages_for_ai)

    def _get_ai_response_with_tools(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        """调用 AI 并支持工具"""
        try:
            if not self.ai_client:
                return {'content': 'AI 服务未配置'}

            # 检查 AI 客户端是否支持工具调用
            url_base = (self.ai_base_url or "").rstrip("/")
            url = f"{url_base}/chat/completions"

            headers = {
                "Authorization": f"Bearer {self.ai_api_key}",
                "Content-Type": "application/json"
            }

            payload = {
                "model": self.ai_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto"
            }

            import requests
            resp = requests.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})

            result = {
                'content': message.get('content', '')
            }

            # 处理工具调用
            if 'tool_calls' in message:
                tool_calls = message['tool_calls']
                result['tool_calls'] = []

                for tool_call in tool_calls:
                    function_name = tool_call.get('function', {}).get('name')
                    arguments = json.loads(tool_call.get('function', {}).get('arguments', '{}'))

                    result['tool_calls'].append({
                        'id': tool_call.get('id', ''),
                        'name': function_name,
                        'arguments': arguments
                    })

            return result

        except Exception as e:
            _log.error(f"AI with tools error: {e}")
            # 回退到普通 AI 调用
            content = self._get_ai_response(messages)
            return {'content': content}

    def _register_routes(self):
        """注册 HTTP 路由"""

        @self.app.route('/')
        def index():
            template_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
            with open(template_path, 'r', encoding='utf-8') as f:
                return f.read(), 200, {'Content-Type': 'text/html'}

        # ==================== 登录 API ====================
        @self.app.route('/api/login', methods=['POST'])
        def login():
            """用户登录验证"""
            data = request.json
            username = data.get('username', '').strip()
            password = data.get('password', '').strip()
            
            if not username:
                return jsonify({'success': False, 'message': '用户名不能为空'}), 400
            
            # 如果设置了密码，需要验证
            if self.web_password:
                if not password:
                    return jsonify({'success': False, 'message': '请输入密码'}), 401
                if password != self.web_password:
                    return jsonify({'success': False, 'message': '密码错误'}), 401
            
            # 登录成功
            return jsonify({'success': True, 'message': '登录成功'})

        # ==================== 会话管理 API ====================
        @self.app.route('/api/sessions')
        def get_sessions():
            # 使用缓存避免频繁读取文件
            current_time = time.time()
            if not hasattr(self, '_sessions_cache'):
                self._sessions_cache = []
                self._sessions_cache_time = 0
            
            # 缓存5秒
            if (current_time - self._sessions_cache_time) < 5.0 and self._sessions_cache:
                return jsonify(self._sessions_cache)
            
            # 从文件读取
            sessions_file = os.path.join(self.data_dir, 'sessions.json')
            sessions_data = {}
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, 'r', encoding='utf-8') as f:
                        sessions_data = json.load(f)
                except:
                    sessions_data = {}
            
            # 合并内存中的会话
            for sid, session in self.sessions.items():
                if sid not in sessions_data:
                    sessions_data[sid] = session
            
            sessions = []
            for sid, session in sessions_data.items():
                # 只返回基本信息，不返回消息内容
                sessions.append({
                    'id': sid,
                    'name': session.get('name', f'会话 {sid[:8]}'),
                    'type': session.get('type', 'web'),
                    'user_id': session.get('user_id'),
                    'qq_id': session.get('qq_id'),
                    'created_at': session.get('created_at'),
                    'message_count': len(session.get('messages', [])),
                    'system_prompt': session.get('system_prompt', '')
                })
            
            # 更新缓存
            self._sessions_cache = sessions
            self._sessions_cache_time = current_time
            
            return jsonify(sessions)

        @self.app.route('/api/sessions', methods=['POST'])
        def create_session():
            data = request.json
            session_id = str(uuid.uuid4())
            
            # 使用人格提示词作为默认系统提示词
            system_prompt = data.get('system_prompt', self.personality.get('prompt', ''))

            session = {
                'id': session_id,
                'name': data.get('name', f'新会话 {session_id[:8]}'),
                'type': data.get('type', 'web'),
                'user_id': data.get('user_id'),
                'created_at': datetime.now().isoformat(),
                'messages': [{"role": "system", "content": system_prompt}],
                'system_prompt': system_prompt
            }

            self.sessions[session_id] = session
            self._save_data('sessions')
            return jsonify({'id': session_id, 'session': session})

        @self.app.route('/api/sessions/<session_id>')
        def get_session(session_id):
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({'error': 'Session not found'}), 404
            return jsonify(session)

        @self.app.route('/api/sessions/<session_id>', methods=['PUT'])
        def update_session(session_id):
            if session_id not in self.sessions:
                return jsonify({'error': 'Session not found'}), 404
            
            data = request.json
            session = self.sessions[session_id]
            session['name'] = data.get('name', session['name'])
            
            # 更新系统提示词
            new_prompt = data.get('system_prompt', session.get('system_prompt', ''))
            if new_prompt != session.get('system_prompt', ''):
                session['system_prompt'] = new_prompt
                # 更新消息列表中的系统消息
                if session['messages'] and session['messages'][0].get('role') == 'system':
                    session['messages'][0]['content'] = new_prompt
                else:
                    session['messages'].insert(0, {"role": "system", "content": new_prompt})
            
            self._save_data('sessions')
            return jsonify({'success': True, 'session': session})

        @self.app.route('/api/sessions/<session_id>', methods=['DELETE'])
        def delete_session(session_id):
            if session_id in self.sessions:
                del self.sessions[session_id]
                self._save_data('sessions')
                return jsonify({'success': True})
            return jsonify({'error': 'Session not found'}), 404

        @self.app.route('/api/sessions/<session_id>/messages', methods=['GET'])
        def get_messages(session_id):
            # 从文件读取
            sessions_file = os.path.join(self.data_dir, 'sessions.json')
            sessions_data = {}
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, 'r', encoding='utf-8') as f:
                        sessions_data = json.load(f)
                except:
                    sessions_data = {}
            
            # 合并内存
            for sid, session in self.sessions.items():
                if sid not in sessions_data:
                    sessions_data[sid] = session
            
            session = sessions_data.get(session_id)
            if not session:
                return jsonify({'error': 'Session not found'}), 404

            limit = request.args.get('limit', 50, type=int)
            messages = session.get('messages', [])[-limit:]
            # 不返回系统消息给前端显示
            display_messages = [m for m in messages if m.get('role') != 'system']
            return jsonify(display_messages)

        @self.app.route('/api/sessions/<session_id>/messages', methods=['POST'])
        def add_message(session_id):
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({'error': 'Session not found'}), 404

            data = request.json
            message = {
                'id': str(uuid.uuid4()),
                'role': data.get('role', 'user'),
                'content': data.get('content', ''),
                'timestamp': datetime.now().isoformat(),
                'sender': data.get('sender', 'web_user')
            }

            session['messages'].append(message)
            return jsonify(message)

        @self.app.route('/api/sessions/<session_id>/messages', methods=['DELETE'])
        def clear_messages(session_id):
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({'error': 'Session not found'}), 404
            
            # 保留系统消息
            system_msg = None
            if session['messages'] and session['messages'][0].get('role') == 'system':
                system_msg = session['messages'][0]
            
            session['messages'] = [system_msg] if system_msg else []
            return jsonify({'success': True})

        @self.app.route('/api/sessions/<session_id>/chat', methods=['POST'])
        def chat_with_ai(session_id):
            """与 AI 对话"""
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({'error': 'Session not found'}), 404
            
            data = request.json
            user_content = data.get('content', '').strip()
            
            if not user_content:
                return jsonify({'error': 'Content is required'}), 400
            
            # 添加用户消息
            user_message = {
                'id': str(uuid.uuid4()),
                'role': 'user',
                'content': user_content,
                'timestamp': datetime.now().isoformat(),
                'sender': data.get('sender', 'web_user')
            }
            session['messages'].append(user_message)
            
            # 获取 AI 回复
            messages_for_ai = session['messages'].copy()
            
            # 限制历史长度
            MAX_HISTORY = 20
            if len(messages_for_ai) > MAX_HISTORY:
                messages_for_ai = [messages_for_ai[0]] + messages_for_ai[-MAX_HISTORY:]
            
            # 异步获取 AI 回复
            def get_response():
                try:
                    assistant_content = self._get_ai_response(messages_for_ai)
                    
                    assistant_message = {
                        'id': str(uuid.uuid4()),
                        'role': 'assistant',
                        'content': assistant_content,
                        'timestamp': datetime.now().isoformat(),
                        'sender': 'AI'
                    }
                    
                    session['messages'].append(assistant_message)
                    
                    # 更新 Token 统计（估算）
                    estimated_tokens = len(user_content) + len(assistant_content)
                    self.token_stats['today'] += estimated_tokens
                    self.token_stats['month'] += estimated_tokens
                    
                    # 通过 WebSocket 发送回复
                    self.socketio.emit('ai_response', {
                        'session_id': session_id,
                        'message': assistant_message
                    }, room=session_id)
                    
                except Exception as e:
                    _log.error(f"Error in AI chat: {e}")
                    error_message = {
                        'id': str(uuid.uuid4()),
                        'role': 'assistant',
                        'content': f'抱歉，处理消息时出错: {str(e)}',
                        'timestamp': datetime.now().isoformat(),
                        'sender': 'AI',
                        'error': True
                    }
                    session['messages'].append(error_message)
                    self.socketio.emit('ai_response', {
                        'session_id': session_id,
                        'message': error_message
                    }, room=session_id)
            
            # 启动后台线程获取 AI 回复
            threading.Thread(target=get_response, daemon=True).start()
            
            return jsonify({'success': True, 'user_message': user_message})

        # ==================== AI 工具调用 API ====================
        @self.app.route('/api/ai/tools', methods=['POST'])
        def ai_tools_handler():
            """AI 工具调用接口 - 让 AI 可以直接操作工作流等功能"""
            data = request.json
            messages = data.get('messages', [])
            
            if not messages:
                return jsonify({'error': 'Messages are required'}), 400
            
            # 定义可用工具
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "create_workflow",
                        "description": "创建一个新的工作流。当用户想要创建定时任务、自动回复等功能时使用。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "工作流名称，简短明了"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "工作流描述，作为系统提示词给AI，详细说明工作流的功能和执行逻辑"
                                },
                                "trigger": {
                                    "type": "string",
                                    "enum": ["manual", "cron", "message"],
                                    "description": "触发方式：manual手动、cron定时、message消息触发"
                                },
                                "cron": {
                                    "type": "string",
                                    "description": "定时触发时的cron表达式，如'0 8 * * *'表示每天8点"
                                },
                                "keywords": {
                                    "type": "string",
                                    "description": "消息触发时的关键词，多个用逗号分隔"
                                },
                                "target_type": {
                                    "type": "string",
                                    "enum": ["none", "qq_group", "qq_private", "session"],
                                    "description": "结果发送目标"
                                },
                                "target_id": {
                                    "type": "string",
                                    "description": "目标ID，如QQ群号、QQ号或会话ID"
                                }
                            },
                            "required": ["name", "description", "trigger"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_workflows",
                        "description": "列出所有工作流，用于查看当前有哪些工作流",
                        "parameters": {
                            "type": "object",
                            "properties": {}
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "toggle_workflow",
                        "description": "启用或禁用工作流",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "workflow_id": {
                                    "type": "string",
                                    "description": "工作流ID"
                                }
                            },
                            "required": ["workflow_id"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "execute_workflow",
                        "description": "立即执行一个工作流",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "workflow_id": {
                                    "type": "string",
                                    "description": "工作流ID"
                                }
                            },
                            "required": ["workflow_id"]
                        }
                    }
                }
            ]
            
            try:
                # 调用 AI 并启用工具
                if not self.ai_client:
                    return jsonify({'error': 'AI client not initialized'}), 503
                
                # 添加系统提示词
                system_prompt = """你是一个智能助手，可以帮助用户管理工作流。你可以使用工具来创建、查看、启用/禁用和执行工作流。

工作流是一种自动化任务，可以：
1. 定时执行（如每天早上8点发送问候）
2. 消息触发（如收到关键词时自动回复）
3. 手动执行

当用户想要创建工作流时，请：
1. 理解用户的需求
2. 使用 create_workflow 工具创建工作流
3. 向用户确认创建结果

当用户询问有哪些工作流时，使用 list_workflows 工具。
当用户想要启用/禁用工作流时，先列出工作流让用户选择，然后使用 toggle_workflow 工具。
当用户想要执行工作流时，使用 execute_workflow 工具。"""
                
                full_messages = [{"role": "system", "content": system_prompt}] + messages
                
                # 调用 AI（支持工具的版本）
                response = self._get_ai_response_with_tools(full_messages, tools)
                
                return jsonify({
                    'content': response.get('content', ''),
                    'tool_calls': response.get('tool_calls', [])
                })
                
            except Exception as e:
                _log.error(f"AI tools error: {e}")
                return jsonify({'error': str(e)}), 500

        def _get_ai_response_with_tools(self, messages: List[Dict], tools: List[Dict]) -> Dict:
            """调用 AI 并支持工具"""
            try:
                # 检查 AI 客户端是否支持工具调用
                url_base = (self.ai_base_url or "").rstrip("/")
                url = f"{url_base}/chat/completions"
                
                headers = {
                    "Authorization": f"Bearer {self.ai_api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": self.ai_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto"
                }
                
                import requests
                resp = requests.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                
                result = {
                    'content': message.get('content', '')
                }
                
                # 处理工具调用
                if 'tool_calls' in message:
                    tool_calls = message['tool_calls']
                    result['tool_calls'] = []
                    
                    for tool_call in tool_calls:
                        function_name = tool_call.get('function', {}).get('name')
                        arguments = json.loads(tool_call.get('function', {}).get('arguments', '{}'))
                        
                        # 执行工具
                        tool_result = self._execute_tool(function_name, arguments)
                        result['tool_calls'].append({
                            'name': function_name,
                            'arguments': arguments,
                            'result': tool_result
                        })
                
                return result
                
            except Exception as e:
                _log.error(f"AI with tools error: {e}")
                # 回退到普通 AI 调用
                content = self._get_ai_response(messages)
                return {'content': content}

        def _execute_tool(self, function_name: str, arguments: Dict) -> Dict:
            """执行工具函数"""
            try:
                if function_name == 'create_workflow':
                    workflow = {
                        'id': str(uuid.uuid4()),
                        'name': arguments['name'],
                        'description': arguments['description'],
                        'enabled': True,
                        'trigger': arguments['trigger'],
                        'config': {
                            'cron': arguments.get('cron', '0 8 * * *'),
                            'keywords': arguments.get('keywords', ''),
                            'target_type': arguments.get('target_type', 'none'),
                            'target_id': arguments.get('target_id', ''),
                            'max_history': 10
                        }
                    }
                    self.workflows.append(workflow)
                    self._save_data('workflows')
                    
                    # 如果是定时触发，添加到调度器
                    if workflow['trigger'] == 'cron':
                        self._schedule_workflow(workflow)
                    
                    return {'success': True, 'workflow': workflow}
                
                elif function_name == 'list_workflows':
                    return {'success': True, 'workflows': self.workflows}
                
                elif function_name == 'toggle_workflow':
                    workflow_id = arguments['workflow_id']
                    for w in self.workflows:
                        if w['id'] == workflow_id:
                            w['enabled'] = not w.get('enabled', True)
                            self._save_data('workflows')
                            
                            if w['trigger'] == 'cron':
                                if w['enabled']:
                                    self._schedule_workflow(w)
                                else:
                                    self._unschedule_workflow(workflow_id)
                            
                            return {'success': True, 'enabled': w['enabled']}
                    return {'success': False, 'error': 'Workflow not found'}
                
                elif function_name == 'execute_workflow':
                    workflow_id = arguments['workflow_id']
                    self._execute_workflow(workflow_id, {'source': 'manual'})
                    return {'success': True}
                
                else:
                    return {'success': False, 'error': f'Unknown function: {function_name}'}
                    
            except Exception as e:
                return {'success': False, 'error': str(e)}

        # ==================== QQ 会话 API ====================
        @self.app.route('/api/qq/sessions')
        def get_qq_sessions():
            """获取所有 QQ 会话（群聊和私聊）"""
            qq_sessions = []
            for session_id, session in self.sessions.items():
                if session.get('type') in ['qq_group', 'qq_private']:
                    qq_sessions.append({
                        'id': session_id,
                        'name': session.get('name', 'QQ 会话'),
                        'type': session.get('type'),
                        'qq_id': session.get('qq_id'),
                        'message_count': len(session.get('messages', [])),
                        'last_message': session.get('last_message'),
                        'created_at': session.get('created_at')
                    })
            return jsonify(qq_sessions)

        @self.app.route('/api/qq/sessions/<session_id>/sync', methods=['POST'])
        def sync_qq_session(session_id):
            """同步 QQ 会话消息"""
            if session_id not in self.sessions:
                return jsonify({'error': 'Session not found'}), 404
            
            session = self.sessions[session_id]
            if session.get('type') not in ['qq_group', 'qq_private']:
                return jsonify({'error': 'Not a QQ session'}), 400
            
            # 从保存的 QQ 消息记录中加载
            try:
                from nbot.chat import user_messages, group_messages
                qq_id = session.get('qq_id')
                
                if session['type'] == 'qq_private' and qq_id in user_messages:
                    messages = user_messages[qq_id]
                    # 转换格式
                    for msg in messages:
                        session['messages'].append({
                            'id': str(uuid.uuid4()),
                            'role': 'user' if msg.get('role') == 'user' else 'assistant',
                            'content': msg.get('content', ''),
                            'timestamp': msg.get('time', datetime.now().isoformat()),
                            'sender': msg.get('sender', 'QQ User')
                        })
                elif session['type'] == 'qq_group' and qq_id in group_messages:
                    messages = group_messages[qq_id]
                    for msg in messages:
                        session['messages'].append({
                            'id': str(uuid.uuid4()),
                            'role': 'user' if msg.get('role') == 'user' else 'assistant',
                            'content': msg.get('content', ''),
                            'timestamp': msg.get('time', datetime.now().isoformat()),
                            'sender': msg.get('sender', 'QQ User')
                        })
                
                self._save_data('sessions')
                return jsonify({'success': True, 'message_count': len(session['messages'])})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/qq/sessions/<session_id>/send', methods=['POST'])
        def send_qq_message(session_id):
            """从 Web 发送消息到 QQ"""
            if session_id not in self.sessions:
                return jsonify({'error': 'Session not found'}), 404
            
            session = self.sessions[session_id]
            if session.get('type') not in ['qq_group', 'qq_private']:
                return jsonify({'error': 'Not a QQ session'}), 400
            
            if not self.qq_bot:
                return jsonify({'error': 'QQ Bot not available'}), 503
            
            data = request.json
            content = data.get('content', '').strip()
            
            if not content:
                return jsonify({'error': 'Content is required'}), 400
            
            try:
                qq_id = session.get('qq_id')
                
                # 使用线程运行异步任务
                import threading
                import asyncio
                
                async def send_qq_msg():
                    try:
                        if session['type'] == 'qq_group':
                            # 发送到群聊
                            await self.qq_bot.api.post_group_msg(group_id=qq_id, text=content)
                        else:
                            # 发送到私聊
                            await self.qq_bot.api.post_private_msg(user_id=qq_id, text=content)
                    except Exception as e:
                        _log.error(f"Failed to send QQ message: {e}")
                
                def run_async_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(send_qq_msg())
                        loop.close()
                    except Exception as e:
                        _log.error(f"Failed to run async task: {e}")
                
                threading.Thread(target=run_async_task, daemon=True).start()
                
                # 记录到会话
                message = {
                    'id': str(uuid.uuid4()),
                    'role': 'assistant',
                    'content': content,
                    'timestamp': datetime.now().isoformat(),
                    'sender': 'Bot'
                }
                session['messages'].append(message)
                session['last_message'] = content
                self._save_data('sessions')
                
                return jsonify({'success': True, 'message': message})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/qq/link-session', methods=['POST'])
        def link_qq_session():
            """将 QQ 聊天关联为 Web 会话"""
            data = request.json
            qq_id = data.get('qq_id')
            session_type = data.get('type')  # 'qq_group' 或 'qq_private'
            name = data.get('name', f'QQ {qq_id}')
            
            if not qq_id or not session_type:
                return jsonify({'error': 'qq_id and type are required'}), 400
            
            # 查找是否已存在
            for session_id, session in self.sessions.items():
                if session.get('qq_id') == qq_id and session.get('type') == session_type:
                    return jsonify({'success': True, 'session_id': session_id, 'exists': True})
            
            # 创建新会话
            session_id = str(uuid.uuid4())
            session = {
                'id': session_id,
                'name': name,
                'type': session_type,
                'qq_id': qq_id,
                'created_at': datetime.now().isoformat(),
                'messages': [],
                'system_prompt': ''
            }
            self.sessions[session_id] = session
            self._save_data('sessions')
            
            return jsonify({'success': True, 'session_id': session_id, 'exists': False})

        # ==================== Heartbeat API ====================
        @self.app.route('/api/heartbeat')
        def get_heartbeat():
            """获取 Heartbeat 配置"""
            return jsonify(self.heartbeat_config)

        @self.app.route('/api/heartbeat', methods=['PUT'])
        def update_heartbeat():
            """更新 Heartbeat 配置"""
            data = request.json
            
            enabled = data.get('enabled', self.heartbeat_config.get('enabled', False))
            interval_minutes = data.get('interval_minutes', self.heartbeat_config.get('interval_minutes', 60))
            content_file = data.get('content_file', self.heartbeat_config.get('content_file', 'heartbeat.md'))
            targets = data.get('targets', self.heartbeat_config.get('targets', []))
            
            self.heartbeat_config = {
                'enabled': enabled,
                'interval_minutes': interval_minutes,
                'content_file': content_file,
                'targets': targets,
                'last_run': self.heartbeat_config.get('last_run'),
                'next_run': self.heartbeat_config.get('next_run')
            }
            
            # 更新调度器
            if enabled:
                self._start_heartbeat_job(interval_minutes)
            else:
                self._stop_heartbeat_job()
            
            self._save_data('heartbeat')
            return jsonify({'success': True, 'config': self.heartbeat_config})

        @self.app.route('/api/heartbeat/run', methods=['POST'])
        def run_heartbeat():
            """手动触发 Heartbeat"""
            try:
                self._execute_heartbeat()
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/heartbeat/content', methods=['GET'])
        def get_heartbeat_content():
            """获取 heartbeat.md 文件内容"""
            content_file = request.args.get('file', 'heartbeat.md')
            content = self._load_heartbeat_content(content_file)
            return jsonify({'content': content, 'file': content_file})

        @self.app.route('/api/heartbeat/content', methods=['PUT'])
        def save_heartbeat_content():
            """保存 heartbeat.md 文件内容"""
            data = request.json
            content = data.get('content', '')
            content_file = data.get('file', 'heartbeat.md')
            
            # 优先保存到 resources 目录
            save_path = os.path.join(os.path.dirname(__file__), '..', '..', 'resources', content_file)
            
            try:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return jsonify({'success': True, 'path': save_path})
            except Exception as e:
                # 尝试备用路径
                try:
                    save_path = os.path.join(os.getcwd(), 'resources', content_file)
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    return jsonify({'success': True, 'path': save_path})
                except Exception as e2:
                    return jsonify({'success': False, 'error': f'无法保存文件: {e}'}), 500

        # ==================== 工作流 API ====================
        @self.app.route('/api/workflows')
        def get_workflows():
            return jsonify(self.workflows)

        @self.app.route('/api/workflows', methods=['POST'])
        def create_workflow():
            data = request.json
            workflow = {
                'id': str(uuid.uuid4()),
                'name': data.get('name', '新工作流'),
                'description': data.get('description', ''),
                'enabled': data.get('enabled', True),
                'trigger': data.get('trigger', 'manual'),
                'config': data.get('config', {})
            }
            self.workflows.append(workflow)
            self._save_data('workflows')
            
            # 如果启用了 cron 触发，添加到调度器
            if workflow.get('enabled') and workflow.get('trigger') == 'cron':
                self._schedule_workflow(workflow)
            
            return jsonify({'success': True, 'workflow': workflow})

        @self.app.route('/api/workflows/<workflow_id>', methods=['PUT'])
        def update_workflow(workflow_id):
            for workflow in self.workflows:
                if workflow['id'] == workflow_id:
                    data = request.json
                    old_trigger = workflow.get('trigger')
                    old_enabled = workflow.get('enabled')
                    
                    workflow.update(data)
                    self._save_data('workflows')
                    
                    # 更新调度器
                    if workflow.get('trigger') == 'cron':
                        if workflow.get('enabled'):
                            self._schedule_workflow(workflow)
                        else:
                            self._unschedule_workflow(workflow_id)
                    elif old_trigger == 'cron':
                        self._unschedule_workflow(workflow_id)
                    
                    return jsonify({'success': True, 'workflow': workflow})
            return jsonify({'error': 'Workflow not found'}), 404

        @self.app.route('/api/workflows/<workflow_id>', methods=['DELETE'])
        def delete_workflow(workflow_id):
            # 取消调度
            self._unschedule_workflow(workflow_id)
            
            self.workflows = [w for w in self.workflows if w['id'] != workflow_id]
            self._save_data('workflows')
            return jsonify({'success': True})

        @self.app.route('/api/workflows/<workflow_id>/toggle', methods=['POST'])
        def toggle_workflow(workflow_id):
            for workflow in self.workflows:
                if workflow['id'] == workflow_id:
                    workflow['enabled'] = not workflow.get('enabled', True)
                    self._save_data('workflows')
                    
                    # 更新调度器
                    if workflow.get('trigger') == 'cron':
                        if workflow['enabled']:
                            self._schedule_workflow(workflow)
                        else:
                            self._unschedule_workflow(workflow_id)
                    
                    return jsonify({'success': True, 'enabled': workflow['enabled']})
            return jsonify({'error': 'Workflow not found'}), 404

        @self.app.route('/api/workflows/<workflow_id>/execute', methods=['POST'])
        def execute_workflow(workflow_id):
            """手动执行工作流"""
            for workflow in self.workflows:
                if workflow['id'] == workflow_id:
                    data = request.json or {}
                    trigger_data = data.get('trigger_data', {'source': 'manual', 'time': datetime.now().isoformat()})
                    self._execute_workflow(workflow_id, trigger_data)
                    return jsonify({'success': True, 'message': 'Workflow execution started'})
            return jsonify({'error': 'Workflow not found'}), 404

        # ==================== 人格设置 API ====================
        @self.app.route('/api/personality')
        def get_personality():
            return jsonify(self.personality)

        @self.app.route('/api/personality', methods=['PUT'])
        def update_personality():
            data = request.json
            self.personality['name'] = data.get('name', self.personality.get('name', ''))
            self.personality['prompt'] = data.get('prompt', self.personality.get('prompt', ''))
            
            # 保存到文件
            try:
                prompt_file = "resources/prompts/neko.txt"
                os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
                with open(prompt_file, "w", encoding="utf-8") as f:
                    f.write(self.personality['prompt'])
            except Exception as e:
                _log.error(f"Failed to save personality: {e}")
            
            return jsonify({'success': True, 'personality': self.personality})

        @self.app.route('/api/personality/presets')
        def get_personality_presets():
            presets = [
                {"id": "1", "name": "猫娘助手", "icon": "🐱", "description": "可爱温柔的猫娘，说话带喵尾音", 
                 "prompt": "你是一只可爱的猫娘，名叫 Neko。你说话时会带「喵」的尾音，性格温柔体贴，喜欢帮助主人解决问题。"},
                {"id": "2", "name": "专业助手", "icon": "👔", "description": "专业、高效、简洁的助手",
                 "prompt": "你是一个专业的 AI 助手，回答简洁明了，注重效率。"},
                {"id": "3", "name": "创意作家", "icon": "✍️", "description": "富有创造力的写作助手",
                 "prompt": "你是一个富有创造力的作家，擅长各种文体，能够帮助用户创作精彩内容。"},
                {"id": "4", "name": "代码专家", "icon": "💻", "description": "精通各种编程语言",
                 "prompt": "你是一个编程专家，精通多种编程语言，能够提供高质量的代码和编程建议。"}
            ]
            return jsonify(presets)

        # ==================== 记忆管理 API ====================
        @self.app.route('/api/memory')
        def get_memory():
            """获取记忆列表，支持按类型筛选"""
            mem_type = request.args.get('type', 'all')
            target_id = request.args.get('target_id', '')
            
            memories = self.memories
            
            if mem_type != 'all':
                memories = [m for m in memories if m.get('type', 'long') == mem_type]
            
            if target_id:
                memories = [m for m in memories if m.get('target_id', '') == target_id]
            
            # 分离长期和短期记忆
            long_term = [m for m in memories if m.get('type', 'long') == 'long']
            short_term = [m for m in memories if m.get('type', 'long') == 'short']
            
            return jsonify({
                'memories': memories,
                'long_term': long_term,
                'short_term': short_term
            })

        @self.app.route('/api/memory', methods=['POST'])
        def add_memory():
            data = request.json
            memory = {
                'id': str(uuid.uuid4()),
                'type': data.get('type', 'long'),
                'key': data.get('key', ''),
                'value': data.get('value', ''),
                'target_id': data.get('target_id', ''),
                'priority': data.get('priority', 'normal'),
                'expire_days': data.get('expire_days', 7),
                'created_at': data.get('created_at', datetime.now().isoformat()),
                'updated_at': data.get('updated_at', datetime.now().isoformat())
            }
            self.memories.append(memory)
            self._save_data('memories')
            return jsonify({'success': True, 'memory': memory})

        @self.app.route('/api/memory/<memory_id>', methods=['PUT'])
        def update_memory(memory_id):
            for mem in self.memories:
                if mem.get('id') == memory_id:
                    data = request.json
                    mem['type'] = data.get('type', mem.get('type', 'long'))
                    mem['key'] = data.get('key', mem['key'])
                    mem['value'] = data.get('value', mem['value'])
                    mem['priority'] = data.get('priority', mem.get('priority', 'normal'))
                    mem['expire_days'] = data.get('expire_days', mem.get('expire_days', 7))
                    mem['target_id'] = data.get('target_id', mem.get('target_id', ''))
                    mem['updated_at'] = datetime.now().isoformat()
                    self._save_data('memories')
                    return jsonify({'success': True, 'memory': mem})
            return jsonify({'error': 'Memory not found'}), 404

        @self.app.route('/api/memory/<memory_id>', methods=['DELETE'])
        def delete_memory(memory_id):
            self.memories = [m for m in self.memories if m.get('id') != memory_id]
            self._save_data('memories')
            return jsonify({'success': True})

        @self.app.route('/api/memory', methods=['DELETE'])
        def clear_all_memory():
            self.memories = []
            self._save_data('memories')
            return jsonify({'success': True})

        @self.app.route('/api/memory/export')
        def export_memory():
            return jsonify({'memories': self.memories})

        # ==================== 知识库 API ====================
        @self.app.route('/api/knowledge')
        def get_knowledge():
            return jsonify(self.knowledge_docs)

        @self.app.route('/api/knowledge', methods=['POST'])
        def add_knowledge():
            data = request.json
            doc = {
                'id': str(uuid.uuid4()),
                'name': data.get('name', ''),
                'type': data.get('type', 'txt'),
                'size': data.get('size', 0),
                'indexed': False,
                'content': data.get('content', ''),
                'created_at': datetime.now().isoformat()
            }
            self.knowledge_docs.append(doc)
            self._save_data('knowledge')
            return jsonify({'success': True, 'document': doc})

        @self.app.route('/api/knowledge/<doc_id>')
        def get_knowledge_doc(doc_id):
            for doc in self.knowledge_docs:
                if doc['id'] == doc_id:
                    return jsonify(doc)
            return jsonify({'error': 'Document not found'}), 404

        @self.app.route('/api/knowledge/<doc_id>', methods=['DELETE'])
        def delete_knowledge(doc_id):
            self.knowledge_docs = [d for d in self.knowledge_docs if d['id'] != doc_id]
            self._save_data('knowledge')
            return jsonify({'success': True})

        @self.app.route('/api/knowledge/<doc_id>/index', methods=['POST'])
        def index_knowledge(doc_id):
            for doc in self.knowledge_docs:
                if doc['id'] == doc_id:
                    doc['indexed'] = True
                    self._save_data('knowledge')
                    return jsonify({'success': True})
            return jsonify({'error': 'Document not found'}), 404

        # ==================== AI 配置 API ====================
        @self.app.route('/api/ai-config')
        def get_ai_config():
            # 返回配置时不包含 API Key
            config = self.ai_config.copy()
            config['api_key'] = '********' if config.get('api_key') else ''
            config['model'] = self.ai_model or config.get('model', 'gpt-4')
            config['base_url'] = self.ai_base_url or config.get('base_url', '')
            return jsonify(config)

        @self.app.route('/api/ai-config', methods=['PUT'])
        def update_ai_config():
            data = request.json
            
            # 更新内存配置
            if data.get('provider'):
                self.ai_config['provider'] = data['provider']
            if data.get('api_key') and data['api_key'] != '********':
                self.ai_config['api_key'] = data['api_key']
                self.ai_api_key = data['api_key']
            if data.get('base_url') is not None:
                self.ai_config['base_url'] = data['base_url']
                self.ai_base_url = data['base_url']
            if data.get('model'):
                self.ai_config['model'] = data['model']
                self.ai_model = data['model']
            if data.get('temperature') is not None:
                self.ai_config['temperature'] = data['temperature']
            if data.get('max_tokens') is not None:
                self.ai_config['max_tokens'] = data['max_tokens']
            if data.get('top_p') is not None:
                self.ai_config['top_p'] = data['top_p']
            
            # 重新初始化 AI 客户端
            if self.ai_api_key and self.ai_base_url:
                try:
                    from nbot.services.ai import AIClient
                    import configparser
                    config = configparser.ConfigParser()
                    config.read('config.ini', encoding='utf-8')
                    
                    self.ai_client = AIClient(
                        api_key=self.ai_api_key,
                        base_url=self.ai_base_url,
                        model=self.ai_model,
                        pic_model=config.get('pic', 'model', fallback=''),
                        search_api_key=config.get('search', 'api_key', fallback=''),
                        search_api_url=config.get('search', 'api_url', fallback=''),
                        video_api=config.get('video', 'api_key', fallback=''),
                        silicon_api_key=config.get('ApiKey', 'silicon_api_key', fallback='')
                    )
                except Exception as e:
                    _log.error(f"Failed to reinitialize AI client: {e}")
            
            # 保存 AI 配置到磁盘
            self._save_data('ai_config')

            return jsonify({'success': True})

        # ==================== 多模型配置管理 API ====================
        @self.app.route('/api/ai-models')
        def get_ai_models():
            """获取所有AI模型配置列表"""
            # 返回时隐藏API Key
            models = []
            for m in self.ai_models:
                model_copy = m.copy()
                if 'api_key' in model_copy:
                    model_copy['api_key'] = '********' if model_copy['api_key'] else ''
                models.append(model_copy)
            return jsonify({
                'models': models,
                'active_model_id': self.active_model_id
            })

        @self.app.route('/api/ai-models', methods=['POST'])
        def create_ai_model():
            """创建新的AI模型配置"""
            data = request.json
            model = {
                'id': str(uuid.uuid4()),
                'name': data.get('name', '新配置'),
                'provider': data.get('provider', 'custom'),
                'api_key': data.get('api_key', ''),
                'base_url': data.get('base_url', ''),
                'model': data.get('model', ''),
                'enabled': data.get('enabled', True),
                'temperature': data.get('temperature', 0.7),
                'max_tokens': data.get('max_tokens', 2000),
                'top_p': data.get('top_p', 0.9),
                'frequency_penalty': data.get('frequency_penalty', 0),
                'presence_penalty': data.get('presence_penalty', 0),
                'system_prompt': data.get('system_prompt', ''),
                'timeout': data.get('timeout', 60),
                'retry_count': data.get('retry_count', 3),
                'stream': data.get('stream', True),
                'enable_memory': data.get('enable_memory', True),
                'image_model': data.get('image_model', ''),
                'search_api_key': data.get('search_api_key', ''),
                'embedding_model': data.get('embedding_model', ''),
                'max_context_length': data.get('max_context_length', 8000),
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat()
            }
            self.ai_models.append(model)
            self._save_data('ai_models')
            return jsonify({'success': True, 'model': model})

        @self.app.route('/api/ai-models/<model_id>', methods=['PUT'])
        def update_ai_model(model_id):
            """更新AI模型配置"""
            for model in self.ai_models:
                if model['id'] == model_id:
                    data = request.json
                    model['name'] = data.get('name', model['name'])
                    model['provider'] = data.get('provider', model['provider'])
                    if data.get('api_key') and data['api_key'] != '********':
                        model['api_key'] = data['api_key']
                    model['base_url'] = data.get('base_url', model['base_url'])
                    model['model'] = data.get('model', model['model'])
                    model['enabled'] = data.get('enabled', model.get('enabled', True))
                    model['temperature'] = data.get('temperature', model.get('temperature', 0.7))
                    model['max_tokens'] = data.get('max_tokens', model.get('max_tokens', 2000))
                    model['top_p'] = data.get('top_p', model.get('top_p', 0.9))
                    model['frequency_penalty'] = data.get('frequency_penalty', model.get('frequency_penalty', 0))
                    model['presence_penalty'] = data.get('presence_penalty', model.get('presence_penalty', 0))
                    model['system_prompt'] = data.get('system_prompt', model.get('system_prompt', ''))
                    model['timeout'] = data.get('timeout', model.get('timeout', 60))
                    model['retry_count'] = data.get('retry_count', model.get('retry_count', 3))
                    model['stream'] = data.get('stream', model.get('stream', True))
                    model['enable_memory'] = data.get('enable_memory', model.get('enable_memory', True))
                    model['image_model'] = data.get('image_model', model.get('image_model', ''))
                    model['search_api_key'] = data.get('search_api_key', model.get('search_api_key', ''))
                    model['embedding_model'] = data.get('embedding_model', model.get('embedding_model', ''))
                    model['max_context_length'] = data.get('max_context_length', model.get('max_context_length', 8000))
                    model['updated_at'] = datetime.now().isoformat()
                    self._save_data('ai_models')
                    return jsonify({'success': True, 'model': model})
            return jsonify({'error': 'Model not found'}), 404

        @self.app.route('/api/ai-models/<model_id>', methods=['DELETE'])
        def delete_ai_model(model_id):
            """删除AI模型配置"""
            # 不能删除当前激活的配置
            if self.active_model_id == model_id:
                return jsonify({'error': 'Cannot delete active model'}), 400
            self.ai_models = [m for m in self.ai_models if m['id'] != model_id]
            self._save_data('ai_models')
            return jsonify({'success': True})

        @self.app.route('/api/ai-models/<model_id>/apply', methods=['POST'])
        def apply_ai_model(model_id):
            """应用指定的AI模型配置"""
            if self._apply_ai_model(model_id):
                return jsonify({'success': True, 'message': 'Model applied successfully'})
            return jsonify({'error': 'Failed to apply model'}), 400

        @self.app.route('/api/ai-models/<model_id>/toggle', methods=['POST'])
        def toggle_ai_model(model_id):
            """启用/禁用AI模型配置"""
            for model in self.ai_models:
                if model['id'] == model_id:
                    model['enabled'] = not model.get('enabled', True)
                    self._save_data('ai_models')
                    return jsonify({'success': True, 'enabled': model['enabled']})
            return jsonify({'error': 'Model not found'}), 404

        @self.app.route('/api/ai-models/<model_id>/clone', methods=['POST'])
        def clone_ai_model(model_id):
            """复制AI模型配置"""
            for model in self.ai_models:
                if model['id'] == model_id:
                    cloned = model.copy()
                    cloned['id'] = str(uuid.uuid4())
                    cloned['name'] = f"{model['name']} (复制)"
                    cloned['is_default'] = False
                    cloned['created_at'] = datetime.now().isoformat()
                    cloned['updated_at'] = datetime.now().isoformat()
                    self.ai_models.append(cloned)
                    self._save_data('ai_models')
                    return jsonify({'success': True, 'model': cloned})
            return jsonify({'error': 'Model not found'}), 404

        @self.app.route('/api/ai-models/<model_id>/test', methods=['POST'])
        def test_ai_model(model_id):
            """测试AI模型配置"""
            for model in self.ai_models:
                if model['id'] == model_id:
                    try:
                        import requests
                        url_base = (model.get('base_url') or "").rstrip("/")
                        if not url_base:
                            return jsonify({'success': False, 'message': 'Base URL is required'})
                        if "/chat/completions" in url_base or "/chatcompletion" in url_base:
                            url = url_base
                        else:
                            url = f"{url_base}/chat/completions"

                        headers = {
                            "Authorization": f"Bearer {model.get('api_key', '')}",
                            "Content-Type": "application/json"
                        }
                        payload = {
                            "model": model.get('model', ''),
                            "messages": [{"role": "user", "content": "Hello"}],
                            "max_tokens": 10
                        }
                        resp = requests.post(url, json=payload, headers=headers, timeout=30)
                        resp.raise_for_status()
                        return jsonify({'success': True, 'message': 'Connection successful'})
                    except Exception as e:
                        return jsonify({'success': False, 'message': str(e)})
            return jsonify({'error': 'Model not found'}), 404

        # ==================== Token 统计 API ====================
        @self.app.route('/api/tokens')
        def get_token_stats():
            # 从文件加载真实的 token 统计
            token_stats_file = os.path.join(self.data_dir, 'token_stats.json')
            real_stats = {}
            if os.path.exists(token_stats_file):
                try:
                    with open(token_stats_file, 'r', encoding='utf-8') as f:
                        real_stats = json.load(f)
                except:
                    real_stats = {}
            
            stats_data = {**self.token_stats, **real_stats}
            
            today_total = stats_data.get('today', 0)
            today_input = 0
            today_output = 0
            
            history = stats_data.get('history', [])
            today_str = datetime.now().strftime('%Y-%m-%d')
            for entry in history:
                if entry.get('date') == today_str:
                    today_input = entry.get('input', 0)
                    today_output = entry.get('output', 0)
                    break
            
            if today_input == 0 and today_output == 0 and today_total > 0:
                today_input = today_total // 2
                today_output = today_total // 2
            
            estimated_cost = round(today_input * 0.000001 + today_output * 0.000008, 4)
            
            message_count = sum(len(s.get('messages', [])) for s in self.sessions.values())
            active_sessions = len([s for s in self.sessions.values() if len(s.get('messages', [])) > 0])
            avg_tokens_per_msg = round(today_total / message_count, 2) if message_count > 0 else 0
            
            history = sorted(history, key=lambda x: x.get('date', ''))[-30:]
            
            if len(history) >= 2:
                prev_entry = history[-2]
                prev_total = prev_entry.get('input', 0) + prev_entry.get('output', 0)
                token_change_val = today_total - prev_total
                token_change = f"+{token_change_val}" if token_change_val >= 0 else f"{token_change_val}"
            else:
                token_change = "+0"
            
            stats = {
                'today': today_total,
                'today_input': today_input,
                'today_output': today_output,
                'month': stats_data.get('month', 0),
                'avg_per_chat': stats_data.get('avg_per_chat', 0),
                'estimated_cost': f"{estimated_cost:.4f}",
                'history': history,
                'message_count': message_count,
                'total_tokens': today_total,
                'avg_tokens_per_msg': avg_tokens_per_msg,
                'avg_response_time': 1.5,
                'active_sessions': active_sessions,
                'message_change': f"+{message_count // 7 if message_count > 0 else 0}",
                'token_change': token_change,
                'cost_change': '+0%',
                'avg_change': '+0%',
                'response_change': '-5%',
                'session_change': f"+{active_sessions}"
            }
            return jsonify(stats)

        @self.app.route('/api/tokens/history')
        def get_token_history():
            return jsonify(self.token_stats.get('history', []))

        @self.app.route('/api/tokens/rankings')
        def get_token_rankings():
            """获取 Token 用量排行榜（使用真实数据）"""
            # 从文件加载真实统计数据
            token_stats_file = os.path.join(self.data_dir, 'token_stats.json')
            real_stats = {}
            if os.path.exists(token_stats_file):
                try:
                    with open(token_stats_file, 'r', encoding='utf-8') as f:
                        real_stats = json.load(f)
                except:
                    real_stats = {}
            
            # 会话排行（使用真实的token统计）
            sessions_data = real_stats.get('sessions', {})
            session_rankings = []
            for session_id, data in sessions_data.items():
                session_rankings.append({
                    'name': session_id,
                    'value': data.get('total', 0),
                    'input': data.get('input', 0),
                    'output': data.get('output', 0)
                })
            session_rankings.sort(key=lambda x: x['value'], reverse=True)
            
            # 模型排行
            model_rankings = [
                {'name': self.ai_model or 'MiniMax-Text-01', 'value': real_stats.get('today', 0)}
            ]
            
            # 用户排行（从会话统计中获取）
            user_rankings = []
            for session_id, data in sessions_data.items():
                session_type = data.get('type', 'web')
                if session_type in ['private', 'group']:
                    user_rankings.append({
                        'name': session_id,
                        'value': data.get('total', 0)
                    })
            user_rankings.sort(key=lambda x: x['value'], reverse=True)
            
            return jsonify({
                'sessions': session_rankings[:10],
                'models': model_rankings,
                'users': user_rankings[:10]
            })

        @self.app.route('/api/tokens/record', methods=['POST'])
        def record_token_usage():
            data = request.json
            tokens = data.get('tokens', 0)
            self.token_stats['today'] += tokens
            self.token_stats['month'] += tokens
            return jsonify({'success': True})

        # ==================== 日志 API ====================
        @self.app.route('/api/logs')
        def get_logs():
            level = request.args.get('level', 'all')
            limit = request.args.get('limit', 100, type=int)
            
            logs = self.system_logs
            if level != 'all':
                logs = [l for l in logs if l['level'] == level]
            
            return jsonify(logs[-limit:])

        @self.app.route('/api/logs', methods=['POST'])
        def add_log():
            data = request.json
            log = {
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'level': data.get('level', 'info'),
                'message': data.get('message', '')
            }
            self.system_logs.append(log)
            if len(self.system_logs) > 1000:
                self.system_logs = self.system_logs[-1000:]
            # 持久化保存日志
            self._save_data('logs')
            return jsonify({'success': True})

        @self.app.route('/api/logs', methods=['DELETE'])
        def clear_logs():
            self.system_logs = []
            self._save_data('logs')
            return jsonify({'success': True})

        # ==================== 系统设置 API ====================
        @self.app.route('/api/settings')
        def get_settings():
            return jsonify(self.settings)

        @self.app.route('/api/settings', methods=['PUT'])
        def update_settings():
            data = request.json
            self.settings.update(data)
            return jsonify({'success': True, 'settings': self.settings})

        # ==================== 统计数据 API ====================
        @self.app.route('/api/stats')
        def get_stats():
            # 使用缓存避免频繁计算
            current_time = time.time()
            if (current_time - self._stats_cache_time) < self._stats_cache_ttl and self._stats_cache:
                return jsonify(self._stats_cache)
            
            # 计算今日消息数（使用缓存后只需每5秒重新计算一次）
            today_messages = 0
            total_messages = 0
            today_str = datetime.now().strftime("%Y-%m-%d")
            for session in self.sessions.values():
                messages = session.get('messages', [])
                msg_count = len(messages)
                total_messages += msg_count
                # 只检查最近的消息是否在今天
                for msg in messages[-100:]:  # 只检查最近100条
                    if today_str in msg.get('timestamp', ''):
                        today_messages += 1
            
            # 计算运行时间
            uptime_seconds = int(time.time() - self.start_time)
            uptime = self._format_uptime(uptime_seconds)
            
            # 获取内存使用（如果可能）
            try:
                import psutil
                process = psutil.Process()
                memory_usage = round(process.memory_info().rss / 1024 / 1024, 1)
            except:
                memory_usage = 42  # 默认值
            
            stats = {
                'today_messages': today_messages,
                'total_messages': total_messages,
                'active_sessions': len(self.sessions),
                'token_usage': self.token_stats.get('today', 0),
                'kb_docs': len(self.knowledge_docs),
                'memory_usage': memory_usage,
                'qq_connected': True,
                'ai_service_status': 'normal' if self.ai_client else 'not_configured',
                'platform_count': 1,  # QQ平台
                'uptime': uptime
            }
            
            # 更新缓存
            self._stats_cache = stats
            self._stats_cache_time = current_time
            
            return jsonify(stats)
        
        @self.app.route('/api/stats/messages')
        def get_message_stats():
            """获取消息历史统计数据"""
            period = request.args.get('period', 'day')  # day, week, month
            
            from collections import defaultdict
            from datetime import datetime, timedelta
            
            now = datetime.now()
            stats = defaultdict(int)
            
            if period == 'day':
                # 过去24小时，按小时统计
                for i in range(24):
                    hour_time = now - timedelta(hours=i)
                    hour_key = hour_time.strftime('%H:00')
                    stats[hour_key] = 0
                
                for session in self.sessions.values():
                    for msg in session.get('messages', []):
                        try:
                            msg_time = datetime.fromisoformat(msg.get('timestamp', '').replace('Z', '+00:00'))
                            if now - msg_time <= timedelta(hours=24):
                                hour_key = msg_time.strftime('%H:00')
                                stats[hour_key] += 1
                        except:
                            pass
                            
                # 按时间顺序排列
                sorted_stats = sorted(stats.items(), key=lambda x: x[0])
                return jsonify({
                    'labels': [item[0] for item in sorted_stats],
                    'values': [item[1] for item in sorted_stats]
                })
                
            elif period == 'week':
                # 过去7天，按天统计
                for i in range(7):
                    day_time = now - timedelta(days=i)
                    day_key = day_time.strftime('%m/%d')
                    stats[day_key] = 0
                
                for session in self.sessions.values():
                    for msg in session.get('messages', []):
                        try:
                            msg_time = datetime.fromisoformat(msg.get('timestamp', '').replace('Z', '+00:00'))
                            if now - msg_time <= timedelta(days=7):
                                day_key = msg_time.strftime('%m/%d')
                                stats[day_key] += 1
                        except:
                            pass
                            
                sorted_stats = sorted(stats.items(), key=lambda x: datetime.strptime(x[0], '%m/%d'))
                return jsonify({
                    'labels': [item[0] for item in sorted_stats],
                    'values': [item[1] for item in sorted_stats]
                })
                
            elif period == 'month':
                # 过去30天，按天统计
                for i in range(30):
                    day_time = now - timedelta(days=i)
                    day_key = day_time.strftime('%m/%d')
                    stats[day_key] = 0
                
                for session in self.sessions.values():
                    for msg in session.get('messages', []):
                        try:
                            msg_time = datetime.fromisoformat(msg.get('timestamp', '').replace('Z', '+00:00'))
                            if now - msg_time <= timedelta(days=30):
                                day_key = msg_time.strftime('%m/%d')
                                stats[day_key] += 1
                        except:
                            pass
                            
                sorted_stats = sorted(stats.items(), key=lambda x: datetime.strptime(x[0], '%m/%d'))
                return jsonify({
                    'labels': [item[0] for item in sorted_stats],
                    'values': [item[1] for item in sorted_stats]
                })
            
            return jsonify({'labels': [], 'values': []})
        
        @self.app.route('/api/stats/platforms')
        def get_platform_stats():
            """获取各平台消息统计数据"""
            from collections import defaultdict
            
            platform_stats = defaultdict(int)
            platform_stats['QQ群消息'] = 0
            platform_stats['QQ私聊'] = 0
            platform_stats['Web会话'] = 0
            
            for session in self.sessions.values():
                session_type = session.get('type', 'web')
                msg_count = len(session.get('messages', []))
                
                if session_type == 'qq_group':
                    platform_stats['QQ群消息'] += msg_count
                elif session_type == 'qq_private':
                    platform_stats['QQ私聊'] += msg_count
                else:
                    platform_stats['Web会话'] += msg_count
            
            # 转换为列表格式
            result = []
            colors = {
                'QQ群消息': '#667eea',
                'QQ私聊': '#4facfe',
                'Web会话': '#43e97b'
            }
            
            for name, value in platform_stats.items():
                if value > 0:  # 只返回有数据的平台
                    result.append({
                        'name': name,
                        'value': value,
                        'itemStyle': {'color': colors.get(name, '#999')}
                    })
            
            # 如果没有数据，返回默认值
            if not result:
                result = [
                    {'name': 'QQ群消息', 'value': 0, 'itemStyle': {'color': '#667eea'}},
                    {'name': 'QQ私聊', 'value': 0, 'itemStyle': {'color': '#4facfe'}},
                    {'name': 'Web会话', 'value': 0, 'itemStyle': {'color': '#43e97b'}}
                ]
            
            return jsonify(result)

        # ==================== 配置文件 API (兼容旧版) ====================
        @self.app.route('/api/config')
        def get_config():
            try:
                with open('config.ini', 'r', encoding='utf-8') as f:
                    content = f.read()
                return jsonify({'content': content})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/config', methods=['POST'])
        def save_config():
            data = request.json
            try:
                with open('config.ini', 'w', encoding='utf-8') as f:
                    f.write(data.get('content', ''))
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # ==================== Skills API ====================
        @self.app.route('/api/skills')
        def get_skills():
            """获取所有 Skills 配置"""
            return jsonify(self.skills_config)

        @self.app.route('/api/skills', methods=['POST'])
        def create_skill():
            """创建新 Skill"""
            data = request.json
            skill = {
                'id': str(uuid.uuid4()),
                'name': data.get('name', ''),
                'description': data.get('description', ''),
                'aliases': data.get('aliases', []),
                'enabled': data.get('enabled', True),
                'parameters': data.get('parameters', {})
            }
            self.skills_config.append(skill)
            self._save_data('skills')
            return jsonify({'success': True, 'skill': skill})

        @self.app.route('/api/skills/<skill_id>', methods=['PUT'])
        def update_skill(skill_id):
            """更新 Skill"""
            data = request.json
            for skill in self.skills_config:
                if skill['id'] == skill_id:
                    skill['name'] = data.get('name', skill['name'])
                    skill['description'] = data.get('description', skill['description'])
                    skill['aliases'] = data.get('aliases', skill['aliases'])
                    skill['enabled'] = data.get('enabled', skill['enabled'])
                    skill['parameters'] = data.get('parameters', skill['parameters'])
                    self._save_data('skills')
                    return jsonify({'success': True, 'skill': skill})
            return jsonify({'error': 'Skill not found'}), 404

        @self.app.route('/api/skills/<skill_id>', methods=['DELETE'])
        def delete_skill(skill_id):
            """删除 Skill"""
            self.skills_config = [s for s in self.skills_config if s['id'] != skill_id]
            self._save_data('skills')
            return jsonify({'success': True})

        @self.app.route('/api/skills/<skill_id>/toggle', methods=['POST'])
        def toggle_skill(skill_id):
            """切换 Skill 启用状态"""
            for skill in self.skills_config:
                if skill['id'] == skill_id:
                    skill['enabled'] = not skill.get('enabled', True)
                    self._save_data('skills')
                    return jsonify({'success': True, 'enabled': skill['enabled']})
            return jsonify({'error': 'Skill not found'}), 404

        # ==================== Tools API ====================
        @self.app.route('/api/tools')
        def get_tools():
            """获取所有 Tools 配置"""
            return jsonify(self.tools_config)

        @self.app.route('/api/tools', methods=['POST'])
        def create_tool():
            """创建新 Tool"""
            data = request.json
            tool = {
                'id': str(uuid.uuid4()),
                'name': data.get('name', ''),
                'description': data.get('description', ''),
                'enabled': data.get('enabled', True),
                'parameters': data.get('parameters', {})
            }
            self.tools_config.append(tool)
            self._save_data('tools')
            return jsonify({'success': True, 'tool': tool})

        @self.app.route('/api/tools/<tool_id>', methods=['PUT'])
        def update_tool(tool_id):
            """更新 Tool"""
            data = request.json
            for tool in self.tools_config:
                if tool['id'] == tool_id:
                    tool['name'] = data.get('name', tool['name'])
                    tool['description'] = data.get('description', tool['description'])
                    tool['enabled'] = data.get('enabled', tool['enabled'])
                    tool['parameters'] = data.get('parameters', tool['parameters'])
                    self._save_data('tools')
                    return jsonify({'success': True, 'tool': tool})
            return jsonify({'error': 'Tool not found'}), 404

        @self.app.route('/api/tools/<tool_id>', methods=['DELETE'])
        def delete_tool(tool_id):
            """删除 Tool"""
            self.tools_config = [t for t in self.tools_config if t['id'] != tool_id]
            self._save_data('tools')
            return jsonify({'success': True})

        @self.app.route('/api/tools/<tool_id>/toggle', methods=['POST'])
        def toggle_tool(tool_id):
            """切换 Tool 启用状态"""
            for tool in self.tools_config:
                if tool['id'] == tool_id:
                    tool['enabled'] = not tool.get('enabled', True)
                    self._save_data('tools')
                    return jsonify({'success': True, 'enabled': tool['enabled']})
            return jsonify({'error': 'Tool not found'}), 404

    def _register_socket_events(self):
        """注册 WebSocket 事件"""

        @self.socketio.on('connect')
        def handle_connect():
            user_id = request.args.get('user_id', 'anonymous')
            self.web_users[request.sid] = user_id
            _log.info(f"Web client connected: {user_id}")

        @self.socketio.on('disconnect')
        def handle_disconnect():
            user_id = self.web_users.pop(request.sid, None)
            session_id = self.active_connections.pop(request.sid, None)
            if session_id:
                leave_room(session_id)
            _log.info(f"Web client disconnected: {user_id}")

        @self.socketio.on('join_session')
        def handle_join_session(data):
            session_id = data.get('session_id')
            if session_id in self.sessions:
                join_room(session_id)
                self.active_connections[request.sid] = session_id
                self.socketio.emit('joined_session', {'session_id': session_id}, room=request.sid)
            else:
                _log.warning(f"Client tried to join non-existent session: {session_id}")

        @self.socketio.on('leave_session')
        def handle_leave_session():
            session_id = self.active_connections.pop(request.sid, None)
            if session_id:
                leave_room(session_id)

        @self.socketio.on('send_message')
        def handle_send_message(data):
            session_id = data.get('session_id')
            content = data.get('content', '')
            sender = data.get('sender', 'web_user')
            attachments = data.get('attachments', [])

            # 处理附件信息
            attachment_info = ''
            if attachments and isinstance(attachments, list):
                for att in attachments:
                    if isinstance(att, dict):
                        att_name = att.get('name', 'unknown')
                        att_type = att.get('type', '')
                        attachment_info += f'\n[附件: {att_name}, 类型: {att_type}]'
            
            # 记录日志
            preview = content[:50] if content else ''
            self.log_message('info', f'收到Web消息 from {sender}: {preview}... {len(attachments)}个附件')

            # 先从文件加载会话
            sessions_file = os.path.join(self.data_dir, 'sessions.json')
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, 'r', encoding='utf-8') as f:
                        file_sessions = json.load(f)
                    # 合并到内存
                    for sid, sess in file_sessions.items():
                        if sid not in self.sessions:
                            self.sessions[sid] = sess
                except:
                    pass

            if session_id not in self.sessions:
                self.socketio.emit('error', {'message': 'Session not found'}, room=request.sid)
                return

            # 构建完整内容（包含附件信息）
            full_content = content + attachment_info
            
            message = {
                'id': str(uuid.uuid4()),
                'role': 'user',
                'content': full_content,
                'timestamp': datetime.now().isoformat(),
                'sender': sender,
                'source': 'web',
                'attachments': attachments
            }

            self.sessions[session_id]['messages'].append(message)
            # 使用 socketio.emit 替代 emit，确保广播到 room
            self.socketio.emit('new_message', message, room=session_id)
            
            # 保存会话到磁盘
            self._save_data('sessions')
            
            # 触发 AI 回复（传递附件信息）
            self._trigger_ai_response(session_id, full_content, sender, attachments)

        @self.socketio.on('typing')
        def handle_typing(data):
            session_id = data.get('session_id')
            emit('user_typing', {'sender': self.web_users.get(request.sid)}, room=session_id)

    def _trigger_ai_response(self, session_id: str, user_content: str, sender: str, attachments=None):
        """触发 AI 回复（支持附件）"""
        # 强制转换为列表
        if not attachments or not isinstance(attachments, list):
            attachments = []
            
        session = self.sessions.get(session_id)
        if not session:
            _log.warning(f"Session not found: {session_id}")
            self.log_message('warning', f'Session not found: {session_id}')
            return
        
        # 检查是否有图片附件
        has_image = False
        try:
            for att in attachments:
                if isinstance(att, dict):
                    att_type = att.get('type', '')
                    if att_type and hasattr(att_type, 'startswith') and att_type.startswith('image/'):
                        has_image = True
                        break
        except:
            attachments = []
        
        self.log_message('info', f'开始生成AI回复 for session {session_id[:8]}... (附件: {len(attachments)}, 图片: {has_image})')
        
        def get_response():
            try:
                messages_for_ai = session['messages'].copy()
                
                # 限制历史长度
                MAX_HISTORY = 20
                if len(messages_for_ai) > MAX_HISTORY:
                    messages_for_ai = [messages_for_ai[0]] + messages_for_ai[-MAX_HISTORY:]
                
                # 检查是否有图片附件，如果有则使用多模态AI
                image_urls = []
                if attachments and isinstance(attachments, list):
                    for att in attachments:
                        if isinstance(att, dict):
                            att_type = att.get('type', '')
                            att_data = att.get('data', '')
                            if isinstance(att_type, str) and att_type.startswith('image/') and att_data:
                                # 使用 data URL 格式
                                image_urls.append(att_data)
                
                if image_urls:
                    # 使用多模态AI处理图片
                    assistant_content = self._get_ai_response_with_images(messages_for_ai, image_urls)
                else:
                    # 非图片附件，只传递文本
                    assistant_content = self._get_ai_response(messages_for_ai)
                
                assistant_message = {
                    'id': str(uuid.uuid4()),
                    'role': 'assistant',
                    'content': assistant_content,
                    'timestamp': datetime.now().isoformat(),
                    'sender': 'AI'
                }
                
                session['messages'].append(assistant_message)
                
                # 更新 Token 统计
                estimated_tokens = len(user_content) + len(assistant_content)
                self.token_stats['today'] += estimated_tokens
                self.token_stats['month'] += estimated_tokens
                
                # 通过 WebSocket 发送回复
                self.socketio.emit('ai_response', {
                    'session_id': session_id,
                    'message': assistant_message
                }, room=session_id)
                
                # 记录日志
                self.log_message('info', f'AI回复完成 for session {session_id[:8]}, tokens: {estimated_tokens}')
                
                # 保存会话和 Token 统计到磁盘
                self._save_data('sessions')
                self._save_data('token_stats')
                
            except Exception as e:
                _log.error(f"Error in AI response: {e}")
                error_message = {
                    'id': str(uuid.uuid4()),
                    'role': 'assistant',
                    'content': f'抱歉，处理消息时出错: {str(e)}',
                    'timestamp': datetime.now().isoformat(),
                    'sender': 'AI',
                    'error': True
                }
                session['messages'].append(error_message)
                self.socketio.emit('ai_response', {
                    'session_id': session_id,
                    'message': error_message
                }, room=session_id)
                # 即使出错也保存会话
                self._save_data('sessions')
        
        # 使用 Flask-SocketIO 的后台任务机制
        self.socketio.start_background_task(get_response)

    def add_message_to_session(
        self,
        session_id: str,
        role: str,
        content: str,
        sender: str,
        source: str = 'qq'
    ):
        """从外部添加消息到会话（QQ 消息同步）"""
        if session_id not in self.sessions:
            return

        message = {
            'id': str(uuid.uuid4()),
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat(),
            'sender': sender,
            'source': source
        }

        self.sessions[session_id]['messages'].append(message)
        self.socketio.emit('new_message', message, room=session_id)

    def create_web_session(self, user_id: str, name: str = None) -> str:
        """创建 Web 会话"""
        session_id = str(uuid.uuid4())
        
        system_prompt = self.personality.get('prompt', '')

        session = {
            'id': session_id,
            'name': name or f'Web 会话 {session_id[:8]}',
            'type': 'web',
            'user_id': user_id,
            'created_at': datetime.now().isoformat(),
            'messages': [{"role": "system", "content": system_prompt}],
            'system_prompt': system_prompt
        }

        self.sessions[session_id] = session
        return session_id

    def _init_heartbeat_scheduler(self):
        """初始化 Heartbeat 调度器"""
        if not self.heartbeat_config.get('enabled'):
            _log.info("Heartbeat is disabled")
            return

        interval = self.heartbeat_config.get('interval_minutes', 60)
        self._start_heartbeat_job(interval)

    def _start_heartbeat_job(self, interval_minutes: int):
        """启动 Heartbeat 定时任务"""
        if not self.scheduler:
            _log.warning("Scheduler not available for heartbeat")
            return

        # 移除旧的 job
        if self.heartbeat_job:
            try:
                self.scheduler.remove_job('heartbeat')
            except:
                pass

        try:
            job = self.scheduler.add_job(
                func=self._execute_heartbeat,
                trigger='interval',
                minutes=interval_minutes,
                id='heartbeat',
                replace_existing=True
            )
            self.heartbeat_job = job
            self.heartbeat_config['next_run'] = job.next_run_time.isoformat() if job.next_run_time else None
            _log.info(f"Heartbeat scheduled every {interval_minutes} minutes")
        except Exception as e:
            _log.error(f"Failed to start heartbeat job: {e}")

    def _stop_heartbeat_job(self):
        """停止 Heartbeat 定时任务"""
        if self.scheduler and self.heartbeat_job:
            try:
                self.scheduler.remove_job('heartbeat')
                self.heartbeat_job = None
                _log.info("Heartbeat job stopped")
            except:
                pass

    def _execute_heartbeat(self):
        """执行 Heartbeat 任务"""
        if not self.heartbeat_config.get('enabled'):
            return

        config = self.heartbeat_config
        content_file = config.get('content_file', 'heartbeat.md')
        targets = config.get('targets', [])

        # 读取 heartbeat.md 内容
        content = self._load_heartbeat_content(content_file)
        if not content:
            _log.warning(f"Heartbeat content file '{content_file}' not found or empty")
            return

        _log.info(f"Executing heartbeat with content from {content_file}")

        # 创建或获取 heartbeat 会话
        session_id = f"heartbeat_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        session = {
            'id': session_id,
            'name': f'Heartbeat {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'type': 'heartbeat',
            'user_id': 'heartbeat',
            'created_at': datetime.now().isoformat(),
            'messages': [
                {"role": "system", "content": "你是一个智能助手，请根据以下任务描述执行相关操作。"},
                {"role": "user", "content": content}
            ],
            'system_prompt': "你是一个智能助手，请根据任务描述执行相关操作。"
        }

        self.sessions[session_id] = session

        # 调用 AI 处理
        try:
            from nbot.services.chat_service import chat as do_chat
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            response = loop.run_in_executor(None, do_chat, content, 'heartbeat', None, session_id, False, None, None)
            response_text = loop.run_until_complete(response)
            loop.close()

            if response_text:
                _log.info(f"Heartbeat AI response: {response_text[:200]}...")

                # 发送响应到目标
                for target in targets:
                    self._send_heartbeat_to_target(target, response_text)

                # 更新会话
                self.sessions[session_id]['messages'].append({
                    "role": "assistant",
                    "content": response_text
                })
                self._save_data('sessions')
        except Exception as e:
            _log.error(f"Error executing heartbeat: {e}")

        # 更新最后运行时间
        self.heartbeat_config['last_run'] = datetime.now().isoformat()
        self._save_data('heartbeat')

    def _load_heartbeat_content(self, filename: str) -> str:
        """加载 heartbeat.md 文件内容"""
        # 优先从 resources 目录加载
        possible_paths = [
            os.path.join(os.path.dirname(__file__), '..', '..', 'resources', filename),
            os.path.join(os.getcwd(), 'resources', filename),
            os.path.join(os.path.dirname(__file__), '..', '..', filename),
            os.path.join(os.getcwd(), filename)
        ]

        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        return f.read().strip()
                except Exception as e:
                    _log.error(f"Failed to read heartbeat file {path}: {e}")

        return ""

    def _send_heartbeat_to_target(self, target: str, content: str):
        """发送 heartbeat 结果到指定目标"""
        try:
            if target.startswith('qq_group:'):
                group_id = target.split(':', 1)[1]
                if self.qq_bot:
                    # 发送消息到 QQ 群
                    self.qq_bot.send_group_message(group_id, content)
                    _log.info(f"Heartbeat sent to group {group_id}")
            elif target.startswith('qq_user:'):
                user_id = target.split(':', 1)[1]
                if self.qq_bot:
                    # 发送消息到 QQ 用户
                    self.qq_bot.send_private_message(user_id, content)
                    _log.info(f"Heartbeat sent to user {user_id}")
            elif target == 'web':
                # 广播到所有 Web 客户端
                if self.socketio:
                    self.socketio.emit('heartbeat', {
                        'content': content,
                        'timestamp': datetime.now().isoformat()
                    })
        except Exception as e:
            _log.error(f"Failed to send heartbeat to {target}: {e}")

    def sync_qq_messages(self, user_id: str, group_id: str = None, create_if_not_exists: bool = True):
        """同步 QQ 消息到 Web 会话"""
        from nbot.services.chat_service import user_messages, group_messages

        target_id = group_id or user_id
        if not target_id:
            return None

        # 私聊才需要创建会话
        session_type = 'qq_group' if group_id else 'qq_private'
        
        # 检查是否已存在该 QQ 会话
        existing_session_id = None
        for sid, session in self.sessions.items():
            if session.get('qq_id') == target_id and session.get('type') == session_type:
                existing_session_id = sid
                break
        
        if existing_session_id:
            session_id = existing_session_id
        elif create_if_not_exists and not group_id:
            # 私聊消息：创建新会话
            session_id = str(uuid.uuid4())
            session = {
                'id': session_id,
                'name': f"私聊 {target_id}",
                'type': 'qq_private',
                'qq_id': target_id,
                'created_at': datetime.now().isoformat(),
                'messages': [],
                'system_prompt': ''
            }
            self.sessions[session_id] = session
            self._save_data('sessions')
        else:
            return None

        # 如果消息已存在，同步到会话
        msg_store = group_messages if group_id else user_messages
        if target_id in msg_store:
            messages = msg_store[target_id]
            for msg in messages:
                if msg.get('role') == 'system':
                    continue

                web_msg = {
                    'id': str(uuid.uuid4()),
                    'role': msg.get('role', 'user'),
                    'content': msg.get('content', ''),
                    'timestamp': msg.get('timestamp', datetime.now().isoformat()),
                    'sender': target_id,
                    'source': 'qq'
                }
                self.sessions[session_id]['messages'].append(web_msg)
            
            # 保存会话数据
            self._save_data('sessions')

        return session_id


def create_web_app(config: Dict[str, Any] = None) -> tuple[Flask, SocketIO]:
    """创建 Flask 应用"""
    app = Flask(__name__, static_folder=None)
    app.config['SECRET_KEY'] = 'nbot-secret-key'
    app.config.update(config or {})

    socketio = SocketIO(app, cors_allowed_origins="*")

    server = WebChatServer(app, socketio)

    return app, socketio, server
