import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from flask import jsonify, request

from nbot.core.model_proxy import (
    model_proxy_request_kwargs,
    normalize_model_proxy_url,
)
from nbot.core.sampling import (
    DEFAULT_CHAT_MAX_TOKENS,
    DEFAULT_VIDEO_MAX_TOKENS,
    DEFAULT_VISION_MAX_TOKENS,
    default_max_tokens_for,
    normalize_token_limit_field,
)
from nbot.web.utils.config_loader import resolve_runtime_api_key

_log = logging.getLogger(__name__)

# 模型用途类型定义
MODEL_PURPOSES = {
    "chat": {"name": "对话模型", "icon": "💬", "description": "用于日常对话和问答"},
    "vision": {"name": "图片理解模型", "icon": "🖼️", "description": "用于识别和理解图片内容"},
    "video": {"name": "视频理解模型", "icon": "🎬", "description": "用于分析视频内容"},
    "tts": {"name": "TTS语音合成", "icon": "🔊", "description": "用于文字转语音"},
    "stt": {"name": "STT语音识别", "icon": "🎤", "description": "用于语音转文字"},
    "embedding": {"name": "向量嵌入模型", "icon": "📊", "description": "用于知识库和语义搜索"},
    "rerank": {
        "name": "检索重排模型",
        "icon": "🎯",
        "description": "用于知识库检索结果重排（RAG rerank），提升召回片段的相关度",
    },
    "image_generation": {"name": "图片生成模型", "icon": "🎨", "description": "用于AI生成图片，如角色立绘"}
}

# rerank 用途的专用字段（其余用途不写入，避免污染配置）
RERANK_FORMATS = ("documents", "texts")
DEFAULT_RERANK_TIMEOUT = 15
DEFAULT_RERANK_WEIGHT = 1.0
MAX_RERANK_TIMEOUT = 120


def normalize_rerank_format(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in RERANK_FORMATS else "documents"


def rerank_config_fields(data: dict, purpose: str) -> dict:
    """抽取 purpose=rerank 的专用配置；其它用途返回空字典。"""
    if purpose != "rerank" or not isinstance(data, dict):
        return {}

    def _number(raw, default, minimum=0.0, maximum=None):
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return default
        if maximum is not None:
            number = min(number, maximum)
        return max(number, minimum)

    extra_body = data.get("rerank_extra_body")
    if isinstance(extra_body, str):
        try:
            extra_body = json.loads(extra_body) if extra_body.strip() else {}
        except json.JSONDecodeError:
            extra_body = {}
    if not isinstance(extra_body, dict):
        extra_body = {}

    return {
        "rerank_path": str(data.get("rerank_path") or "").strip(),
        "rerank_format": normalize_rerank_format(data.get("rerank_format")),
        "rerank_timeout": _number(
            data.get("rerank_timeout"),
            DEFAULT_RERANK_TIMEOUT,
            minimum=1.0,
            maximum=MAX_RERANK_TIMEOUT,
        ),
        "rerank_weight": _number(
            data.get("rerank_weight"),
            DEFAULT_RERANK_WEIGHT,
            minimum=0.0,
            maximum=1.0,
        ),
        "rerank_extra_body": extra_body,
    }

# 各用途的默认配置
DEFAULT_PURPOSE_CONFIGS = {
    "chat": {
        "temperature": 0.7,
        "max_tokens": DEFAULT_CHAT_MAX_TOKENS,
        "top_p": 0.9,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_stream": True,
        "system_prompt": ""
    },
    "vision": {
        "temperature": 0.5,
        "max_tokens": DEFAULT_VISION_MAX_TOKENS,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_stream": True,
        "system_prompt": "请详细描述这张图片的内容。"
    },
    "video": {
        "temperature": 0.5,
        "max_tokens": DEFAULT_VIDEO_MAX_TOKENS,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_stream": True,
        "system_prompt": "请分析这个视频的内容。"
    },
    "tts": {
        "voice": "default",
        "speed": 1.0,
        "pitch": 1.0,
        "volume": 1.0
    },
    "stt": {
        "language": "zh",
        "model": "tiny",
        "stt_provider": "",
        "stt_model": "",
        "stt_url": "",
        "stt_headers": ""
    },
    "embedding": {
        "model": "text-embedding-3-small",
        "dimensions": 1536
    },
    "rerank": {
        "temperature": 0,
        "max_tokens": 0,
        "top_p": 1,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_stream": False,
        "system_prompt": "",
        "rerank_path": "",
        "rerank_format": "documents",
        "rerank_timeout": DEFAULT_RERANK_TIMEOUT,
        "rerank_weight": DEFAULT_RERANK_WEIGHT,
    },
    "image_generation": {
        "model": "dall-e-3",
        "size": "1024x1024",
        "quality": "standard",
        "style": "vivid"
    }
}


def register_ai_model_routes(app, server):
    def _refresh_rerank_service(reason: str) -> None:
        """rerank 模型增删改后立刻刷新重排服务（配置读取有 30s 缓存）。"""
        try:
            from nbot.core.knowledge.rerank import get_rerank_service

            get_rerank_service(force_reload=True)
        except Exception as exc:  # pragma: no cover - 知识库未启用时忽略
            _log.debug("[Rerank] %s 后刷新重排服务失败: %s", reason, exc)

    @app.route("/api/ai-models")
    def get_ai_models():
        models = []
        for model in server.ai_models:
            model_copy = model.copy()
            if "api_key" in model_copy:
                model_copy["api_key"] = "********" if model_copy["api_key"] else ""
            # 音色复刻参考音频体积较大，GET 时脱敏（有数据时标记为占位符）
            if model_copy.get("tts_ref_audio"):
                model_copy["tts_ref_audio"] = "__HAS_DATA__"
            models.append(model_copy)
        return jsonify({"models": models, "active_model_id": server.active_model_id})

    @app.route("/api/ai-models", methods=["POST"])
    def create_ai_model():
        data = request.json or {}
        now = datetime.now().isoformat()
        try:
            proxy_url = normalize_model_proxy_url(data.get("proxy_url", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # 服务端校验：参考音频 base64 大小上限（~10MB base64 ≈ 7.5MB 原始文件）
        ref_audio = data.get("tts_ref_audio", "")
        if ref_audio and len(ref_audio) > 15 * 1024 * 1024:
            return jsonify({"error": "参考音频过大，base64 编码后不能超过 10MB"}), 400
        
        # 获取模型用途，默认为对话模型
        purpose = data.get("purpose", "chat")
        # 获取该用途的默认配置
        default_config = DEFAULT_PURPOSE_CONFIGS.get(purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
        
        # 模型价格配置（人民币 元/百万token，null 表示使用兜底定价）
        raw_input_price = data.get("input_price")
        raw_output_price = data.get("output_price")
        raw_cached_input_price = data.get("cached_input_price")

        model = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", f"新{purpose}配置"),
            "purpose": purpose,  # 新增：模型用途
            "provider": data.get("provider", "custom"),
            "provider_type": data.get(
                "provider_type",
                data.get("provider", "openai_compatible"),
            ),
            "api_key": data.get("api_key", ""),
            "base_url": data.get("base_url", ""),
            "proxy_url": proxy_url,
            "append_base_url_path": data.get("append_base_url_path", True),
            "model": data.get("model", ""),
            "enabled": data.get("enabled", True),
            "supports_tools": data.get("supports_tools", default_config.get("supports_tools", True)),
            "supports_reasoning": data.get("supports_reasoning", default_config.get("supports_reasoning", True)),
            "supports_stream": data.get("supports_stream", default_config.get("supports_stream", True)),
            "temperature": data.get("temperature", default_config.get("temperature", 0.7)),
            "max_tokens": data.get("max_tokens", default_config.get("max_tokens", 8192)),
            "top_p": data.get("top_p", default_config.get("top_p", 0.9)),
            "token_limit_field": normalize_token_limit_field(data.get("token_limit_field")),
            "stop": data.get("stop", default_config.get("stop", "")),
            "frequency_penalty": data.get("frequency_penalty", 0),
            "presence_penalty": data.get("presence_penalty", 0),
            "system_prompt": data.get("system_prompt", default_config.get("system_prompt", "")),
            "timeout": data.get("timeout", 60),
            "retry_count": data.get("retry_count", 3),
            "stream": data.get("stream", True),
            "enable_memory": data.get("enable_memory", True),
            "image_model": data.get("image_model", ""),
            "search_api_key": data.get("search_api_key", ""),
            "embedding_model": data.get("embedding_model", default_config.get("model", "") if purpose == "embedding" else ""),
            "max_context_length": data.get("max_context_length", 100000),
            # 模型价格（null 表示使用兜底定价）
            "input_price": raw_input_price if raw_input_price not in (None, "", "null") else None,
            "output_price": raw_output_price if raw_output_price not in (None, "", "null") else None,
            "cached_input_price": (
                raw_cached_input_price
                if raw_cached_input_price not in (None, "", "null")
                else None
            ),
            # TTS 统一配置字段
            "tts_provider": data.get("tts_provider", "openai"),
            "tts_url": data.get("tts_url", ""),
            "tts_model": data.get("tts_model", ""),
            "tts_voice": data.get("tts_voice", default_config.get("tts_voice", "default")),
            "tts_speed": data.get("tts_speed", default_config.get("tts_speed", 1.0)),
            "tts_pitch": data.get("tts_pitch", default_config.get("tts_pitch", 1.0)),
            "tts_volume": data.get("tts_volume", default_config.get("tts_volume", 1.0)),
            "tts_format": data.get("tts_format", "mp3"),
            "tts_upload_url": data.get("tts_upload_url", ""),
            "tts_headers": data.get("tts_headers", ""),
            "tts_body_template": data.get("tts_body_template", ""),
            "tts_ref_audio": data.get("tts_ref_audio", ""),
            "tts_user": data.get("tts_user", ""),
            "language": data.get("language", default_config.get("language", "zh")),
            # STT 统一配置字段
            "stt_provider": data.get("stt_provider", ""),
            "stt_url": data.get("stt_url", ""),
            "stt_model": data.get("stt_model", ""),
            "stt_language": data.get("stt_language", data.get("language", default_config.get("language", "zh"))),
            "stt_headers": data.get("stt_headers", ""),
            "dimensions": data.get("dimensions", default_config.get("dimensions", 1536)),
            # 故障转移优先级（数值越小优先级越高）
            "priority": data.get("priority", 0),
            # 故障转移超时（秒），0 表示使用默认 120s
            "failover_timeout": data.get("failover_timeout", 0) or 0,
            # 图片生成特有配置
            "size": data.get("size", default_config.get("size", "1024x1024")),
            "prompt_template": data.get("prompt_template", ""),
            "created_at": now,
            "updated_at": now,
            # OAuth 账号关联（绑定后 api_key 在运行时由 OAuthManager 注入）
            "oauth_account_id": data.get("oauth_account_id", ""),
        }
        # rerank 用途的专用配置（路径/请求格式/超时/混合权重）
        model.update(rerank_config_fields(data, purpose))
        server.ai_models.append(model)
        server._save_data("ai_models")
        if purpose == "rerank":
            _refresh_rerank_service("新建 rerank 模型")
        return jsonify({"success": True, "model": model})

    @app.route("/api/ai-models/<model_id>", methods=["PUT"])
    def update_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue

            data = request.json or {}
            model["name"] = data.get("name", model["name"])
            # 支持修改模型用途
            if "purpose" in data:
                old_purpose = model.get("purpose", "chat")
                new_purpose = data["purpose"]
                if old_purpose != new_purpose:
                    # 用途变更时，应用新用途的默认配置
                    model["purpose"] = new_purpose
                    default_config = DEFAULT_PURPOSE_CONFIGS.get(new_purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
                    model["supports_tools"] = default_config.get("supports_tools", True)
                    model["supports_reasoning"] = default_config.get("supports_reasoning", True)
                    model["supports_stream"] = default_config.get("supports_stream", True)
                    model["temperature"] = default_config.get("temperature", 0.7)
                    model["max_tokens"] = default_config.get(
                    "max_tokens", default_max_tokens_for(new_purpose)
                )
                    model["top_p"] = default_config.get("top_p", 0.9)
                    model["system_prompt"] = default_config.get("system_prompt", "")
            
            model["provider"] = data.get("provider", model["provider"])
            model["provider_type"] = data.get(
                "provider_type",
                model.get("provider_type", model.get("provider", "openai_compatible")),
            )
            if data.get("api_key") and data["api_key"] != "********":
                model["api_key"] = data["api_key"]
            model["base_url"] = data.get("base_url", model["base_url"])
            if "proxy_url" in data:
                try:
                    model["proxy_url"] = normalize_model_proxy_url(data["proxy_url"])
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400
            model["append_base_url_path"] = data.get(
                "append_base_url_path",
                model.get("append_base_url_path", True),
            )
            model["model"] = data.get("model", model["model"])
            model["enabled"] = data.get("enabled", model.get("enabled", True))
            model["supports_tools"] = data.get(
                "supports_tools", model.get("supports_tools", True)
            )
            model["supports_reasoning"] = data.get(
                "supports_reasoning", model.get("supports_reasoning", True)
            )
            model["supports_stream"] = data.get(
                "supports_stream", model.get("supports_stream", True)
            )
            model["temperature"] = data.get("temperature", model.get("temperature", 0.7))
            model["max_tokens"] = data.get("max_tokens", model.get("max_tokens", 8192))
            model["top_p"] = data.get("top_p", model.get("top_p", 0.9))
            if "token_limit_field" in data:
                model["token_limit_field"] = normalize_token_limit_field(
                    data.get("token_limit_field")
                )
            model["stop"] = data.get("stop", model.get("stop", ""))
            model["frequency_penalty"] = data.get(
                "frequency_penalty", model.get("frequency_penalty", 0)
            )
            model["presence_penalty"] = data.get(
                "presence_penalty", model.get("presence_penalty", 0)
            )
            model["system_prompt"] = data.get(
                "system_prompt", model.get("system_prompt", "")
            )
            model["timeout"] = data.get("timeout", model.get("timeout", 60))
            model["retry_count"] = data.get("retry_count", model.get("retry_count", 3))
            model["stream"] = data.get("stream", model.get("stream", True))
            model["enable_memory"] = data.get(
                "enable_memory", model.get("enable_memory", True)
            )
            model["image_model"] = data.get("image_model", model.get("image_model", ""))
            model["search_api_key"] = data.get(
                "search_api_key", model.get("search_api_key", "")
            )
            model["embedding_model"] = data.get(
                "embedding_model", model.get("embedding_model", "")
            )
            model["max_context_length"] = data.get(
                "max_context_length", model.get("max_context_length", 8000)
            )
            # 模型价格
            if "input_price" in data:
                raw = data["input_price"]
                model["input_price"] = None if raw in (None, "", "null") else raw
            if "output_price" in data:
                raw = data["output_price"]
                model["output_price"] = None if raw in (None, "", "null") else raw
            if "cached_input_price" in data:
                raw = data["cached_input_price"]
                model["cached_input_price"] = None if raw in (None, "", "null") else raw
            # TTS 统一配置字段
            model["tts_provider"] = data.get("tts_provider", model.get("tts_provider", "openai"))
            model["tts_url"] = data.get("tts_url", model.get("tts_url", ""))
            model["tts_model"] = data.get("tts_model", model.get("tts_model", ""))
            model["tts_voice"] = data.get("tts_voice", model.get("tts_voice", "default"))
            model["tts_speed"] = data.get("tts_speed", model.get("tts_speed", 1.0))
            model["tts_pitch"] = data.get("tts_pitch", model.get("tts_pitch", 1.0))
            model["tts_volume"] = data.get("tts_volume", model.get("tts_volume", 1.0))
            model["tts_format"] = data.get("tts_format", model.get("tts_format", "mp3"))
            model["tts_upload_url"] = data.get("tts_upload_url", model.get("tts_upload_url", ""))
            model["tts_headers"] = data.get("tts_headers", model.get("tts_headers", ""))
            model["tts_body_template"] = data.get("tts_body_template", model.get("tts_body_template", ""))
            # 参考音频：__HAS_DATA__ 表示保留已有值（GET 时脱敏的占位符）
            new_ref_audio = data.get("tts_ref_audio", model.get("tts_ref_audio", ""))
            if new_ref_audio == "__HAS_DATA__":
                new_ref_audio = model.get("tts_ref_audio", "")
            elif new_ref_audio and len(new_ref_audio) > 15 * 1024 * 1024:
                return jsonify({"error": "参考音频过大，base64 编码后不能超过 10MB"}), 400
            model["tts_ref_audio"] = new_ref_audio
            model["tts_user"] = data.get("tts_user", model.get("tts_user", ""))
            model["language"] = data.get("language", model.get("language", "zh"))
            # rerank 用途的专用配置（用途切走时保留原值，切回不用重填）
            model.update(rerank_config_fields(data, model.get("purpose", "chat")))
            # STT 统一配置字段
            model["stt_provider"] = data.get("stt_provider", model.get("stt_provider", ""))
            model["stt_url"] = data.get("stt_url", model.get("stt_url", ""))
            model["stt_model"] = data.get("stt_model", model.get("stt_model", ""))
            model["stt_language"] = data.get("stt_language", model.get("stt_language", model.get("language", "zh")))
            model["stt_headers"] = data.get("stt_headers", model.get("stt_headers", ""))
            model["dimensions"] = data.get("dimensions", model.get("dimensions", 1536))
            # 故障转移优先级
            model["priority"] = data.get("priority", model.get("priority", 0))
            # 故障转移超时（秒）
            model["failover_timeout"] = data.get(
                "failover_timeout",
                model.get("failover_timeout", 0),
            ) or 0
            # 图片生成特有配置
            model["size"] = data.get("size", model.get("size", "1024x1024"))
            model["prompt_template"] = data.get("prompt_template", model.get("prompt_template", ""))
            # OAuth 账号关联（绑定后 api_key 在运行时由 OAuthManager 注入）
            model["oauth_account_id"] = data.get(
                "oauth_account_id", model.get("oauth_account_id", "")
            )
            model["updated_at"] = datetime.now().isoformat()
            server._save_data("ai_models")
            if model.get("purpose", "chat") == "rerank":
                _refresh_rerank_service("更新 rerank 模型")
            return jsonify({"success": True, "model": model})

        return jsonify({"error": "Model not found"}), 404

    # ========== 模型用途相关API ==========
    
    @app.route("/api/ai-models/purposes")
    def get_model_purposes():
        """获取所有模型用途类型列表"""
        return jsonify({
            "success": True,
            "purposes": MODEL_PURPOSES,
            "default_configs": DEFAULT_PURPOSE_CONFIGS
        })

    @app.route("/api/ai-models/protocols")
    def get_model_protocols():
        """获取所有已注册的协议类型列表"""
        from nbot.core.protocols import list_protocols
        return jsonify({
            "success": True,
            "protocols": list_protocols(),
        })

    @app.route("/api/ai-models/proxy-settings", methods=["PUT"])
    def update_model_proxy_settings():
        """批量保存每个模型的专用代理链接。"""
        data = request.json or {}
        settings = data.get("settings")
        if not isinstance(settings, list):
            return jsonify({"error": "settings must be a list"}), 400

        models_by_id = {model.get("id"): model for model in server.ai_models}
        normalized_settings = []
        for item in settings:
            if not isinstance(item, dict):
                return jsonify({"error": "invalid proxy setting"}), 400
            model_id = item.get("model_id")
            if model_id not in models_by_id:
                return jsonify({"error": f"Model not found: {model_id}"}), 404
            try:
                proxy_url = normalize_model_proxy_url(item.get("proxy_url", ""))
            except ValueError as exc:
                model_name = models_by_id[model_id].get("name", model_id)
                return jsonify({"error": f"{model_name}: {exc}"}), 400
            normalized_settings.append((model_id, proxy_url))

        now = datetime.now().isoformat()
        for model_id, proxy_url in normalized_settings:
            model = models_by_id[model_id]
            model["proxy_url"] = proxy_url
            model["updated_at"] = now

        server._save_data("ai_models")

        active_model_id = getattr(server, "active_model_id", None)
        active_model = models_by_id.get(active_model_id)
        if active_model is not None:
            active_proxy_url = active_model.get("proxy_url", "")
            if hasattr(server, "ai_config"):
                server.ai_config["proxy_url"] = active_proxy_url
            ai_client = getattr(server, "ai_client", None)
            if ai_client is not None:
                ai_client.proxy_url = active_proxy_url

        return jsonify({"success": True, "updated": len(normalized_settings)})

    @app.route("/api/ai-models/by-purpose/<purpose>")
    def get_models_by_purpose(purpose):
        """按用途获取模型配置列表"""
        if purpose not in MODEL_PURPOSES:
            return jsonify({"error": "Invalid purpose"}), 400
        
        models = []
        for model in server.ai_models:
            if model.get("purpose", "chat") == purpose:
                model_copy = model.copy()
                if "api_key" in model_copy:
                    model_copy["api_key"] = "********" if model_copy["api_key"] else ""
                if model_copy.get("tts_ref_audio"):
                    model_copy["tts_ref_audio"] = "__HAS_DATA__"
                models.append(model_copy)
        
        return jsonify({
            "success": True,
            "purpose": purpose,
            "purpose_info": MODEL_PURPOSES.get(purpose),
            "models": models
        })
    
    @app.route("/api/ai-models/active-by-purpose")
    def get_active_models_by_purpose():
        """获取当前各用途的活跃模型配置"""
        active_models = {}
        
        for purpose in MODEL_PURPOSES.keys():
            # 首先检查是否有明确设置的活跃模型ID
            active_model_id = server.active_models_by_purpose.get(purpose)
            active_model = None
            
            if active_model_id:
                # 查找指定ID的模型
                for model in server.ai_models:
                    if model.get("id") == active_model_id and model.get("enabled", True):
                        active_model = model.copy()
                        if "api_key" in active_model:
                            active_model["api_key"] = "********" if active_model["api_key"] else ""
                        if active_model.get("tts_ref_audio"):
                            active_model["tts_ref_audio"] = "__HAS_DATA__"
                        break

            # 如果没有找到，使用第一个可用的该用途模型
            if not active_model:
                for model in server.ai_models:
                    if model.get("purpose", "chat") == purpose and model.get("enabled", True):
                        active_model = model.copy()
                        if "api_key" in active_model:
                            active_model["api_key"] = "********" if active_model["api_key"] else ""
                        if active_model.get("tts_ref_audio"):
                            active_model["tts_ref_audio"] = "__HAS_DATA__"
                        # 自动设置该用途的活跃模型
                        server.active_models_by_purpose[purpose] = model.get("id")
                        break
            
            active_models[purpose] = {
                "purpose_info": MODEL_PURPOSES.get(purpose),
                "model": active_model,
                "has_config": active_model is not None,
                "active_model_id": active_model.get("id") if active_model else None
            }
        
        return jsonify({
            "success": True,
            "active_models": active_models
        })
    
    @app.route("/api/ai-models/<model_id>/set-purpose", methods=["POST"])
    def set_model_purpose(model_id):
        """设置模型用途"""
        data = request.json or {}
        new_purpose = data.get("purpose")
        
        if not new_purpose or new_purpose not in MODEL_PURPOSES:
            return jsonify({"error": "Invalid or missing purpose"}), 400
        
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            
            old_purpose = model.get("purpose", "chat")
            if old_purpose != new_purpose:
                model["purpose"] = new_purpose
                # 应用新用途的默认配置
                default_config = DEFAULT_PURPOSE_CONFIGS.get(new_purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
                model["supports_tools"] = default_config.get("supports_tools", True)
                model["supports_reasoning"] = default_config.get("supports_reasoning", True)
                model["supports_stream"] = default_config.get("supports_stream", True)
                model["temperature"] = default_config.get("temperature", 0.7)
                model["max_tokens"] = default_config.get(
                    "max_tokens", default_max_tokens_for(new_purpose)
                )
                model["top_p"] = default_config.get("top_p", 0.9)
                model["system_prompt"] = default_config.get("system_prompt", "")
                model["updated_at"] = datetime.now().isoformat()
                server._save_data("ai_models")
                if new_purpose == "rerank":
                    _refresh_rerank_service("切换模型用途")
            model_copy = model.copy()
            if "api_key" in model_copy:
                model_copy["api_key"] = "********" if model_copy["api_key"] else ""
            if model_copy.get("tts_ref_audio"):
                model_copy["tts_ref_audio"] = "__HAS_DATA__"

            return jsonify({
                "success": True,
                "message": f"Model purpose changed from {old_purpose} to {new_purpose}",
                "model": model_copy
            })
        
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>", methods=["DELETE"])
    def delete_ai_model(model_id):
        if server.active_model_id == model_id:
            return jsonify({"error": "Cannot delete active model"}), 400
        removed_purpose = ""
        for m in server.ai_models:
            if m["id"] == model_id:
                removed_purpose = m.get("purpose", "chat")
                break
        server.ai_models = [m for m in server.ai_models if m["id"] != model_id]
        server._save_data("ai_models")
        if removed_purpose == "rerank":
            _refresh_rerank_service("删除 rerank 模型")

        # 记录模型删除操作到 Gateway 日志
        try:
            server.record_operation(
                module="ai_model",
                action="delete",
                description=f"删除 AI 模型 → {model_id[:8]}",
                detail=f"已删除模型 ID: {model_id}",
            )
        except Exception:
            pass

        return jsonify({"success": True})

    @app.route("/api/ai-models/<model_id>/apply", methods=["POST"])
    def apply_ai_model(model_id):
        """应用指定的AI模型配置
        
        请求体中可以指定purpose参数，如果不指定则自动从模型配置中获取
        """
        data = request.json or {}
        purpose = data.get("purpose")
        
        if server._apply_ai_model(model_id, purpose=purpose):
            # 获取应用的模型信息
            model = None
            for m in server.ai_models:
                if m["id"] == model_id:
                    model = m
                    break
            model_purpose = purpose or (model.get("purpose", "chat") if model else "chat")
            purpose_name = MODEL_PURPOSES.get(model_purpose, {}).get("name", model_purpose)
            if model_purpose == "rerank":
                _refresh_rerank_service("应用 rerank 模型")

            # 记录模型切换操作到 Gateway 日志
            try:
                old_model_name = server.active_model_id or "无"
                for m in server.ai_models:
                    if m.get("id") == (server.active_model_id or ""):
                        old_model_name = m.get("name", old_model_name)
                        break
                new_model_name = model.get("name", "Unknown") if model else "Unknown"
                server.record_operation(
                    module="ai_model",
                    action="switch",
                    description=f"切换模型 → {new_model_name}（{purpose_name}）",
                    detail=f"从 {old_model_name} 切换到 {new_model_name}, 用途={purpose_name}",
                    metadata={"model_id": model_id, "model_name": new_model_name, "purpose": model_purpose},
                )
            except Exception:
                pass

            return jsonify({
                "success": True, 
                "message": f"已应用 {purpose_name} 配置: {model.get('name', 'Unknown') if model else 'Unknown'}",
                "purpose": model_purpose,
                "model_id": model_id
            })
        return jsonify({"error": "Failed to apply model"}), 400

    @app.route("/api/ai-models/<model_id>/toggle", methods=["POST"])
    def toggle_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            model["enabled"] = not model.get("enabled", True)
            server._save_data("ai_models")
            return jsonify({"success": True, "enabled": model["enabled"]})
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>/clone", methods=["POST"])
    def clone_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            cloned = model.copy()
            cloned["id"] = str(uuid.uuid4())
            cloned["name"] = f"{model['name']}（副本）"
            cloned["is_default"] = False
            cloned["created_at"] = datetime.now().isoformat()
            cloned["updated_at"] = datetime.now().isoformat()
            server.ai_models.append(cloned)
            server._save_data("ai_models")
            response = cloned.copy()
            if response.get("api_key"):
                response["api_key"] = "********"
            if response.get("tts_ref_audio"):
                response["tts_ref_audio"] = "__HAS_DATA__"
            return jsonify({"success": True, "model": response})
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/fetch-models", methods=["POST"])
    def fetch_available_models():
        """获取可用的模型列表

        根据提供的 API Key、Base URL 和协议类型，自动获取可用的模型列表。
        支持 OpenAI 兼容 API（GET /v1/models）和 Google Gemini API。
        """
        data = request.json or {}
        api_key = data.get("api_key", "")
        selected_key_id = data.get("selectedApiKeyId", "")
        base_url = data.get("base_url", "")
        provider_type = data.get("provider_type", "openai_compatible")
        append_base_url_path = data.get("append_base_url_path", True)
        proxy_url = data.get("proxy_url", "")

        try:
            proxy_kwargs = model_proxy_request_kwargs(proxy_url)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc), "models": []}), 400

        if not base_url:
            return jsonify({"success": False, "message": "Base URL is required", "models": []})

        # 优先使用 API 管理器中选择的 Key
        if selected_key_id:
            from nbot.web.routes.api_keys import load_api_keys
            api_keys = load_api_keys(server)
            selected_key = next((k for k in api_keys if k.get("id") == selected_key_id), None)
            if selected_key and selected_key.get("key"):
                api_key = selected_key["key"]

        # 如果 API Key 为空或者是脱敏的星号，则使用 resolve_runtime_api_key 解析实际的 Key
        if not api_key or api_key == "********":
            api_key = resolve_runtime_api_key(api_key, provider_type)

        if not api_key:
            return jsonify({"success": False, "message": "API Key is required", "models": []})

        try:
            import requests

            # 根据协议类型构建模型列表请求 URL
            if provider_type == "gemini_native":
                # Google Gemini API
                url = f"{base_url.rstrip('/')}/v1beta/models"
                headers = {
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json"
                }
            elif provider_type == "anthropic":
                # Anthropic API - 使用标准的 /v1/models 端点
                base = base_url.rstrip("/")
                # Anthropic 的 base URL 通常是 https://api.anthropic.com
                if not base.endswith("/v1"):
                    url = f"{base}/v1/models"
                else:
                    url = f"{base}/models"
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                }
            else:
                # OpenAI 兼容 API（包括 openai_compatible、openai_responses 等）
                base = base_url.rstrip("/")
                if append_base_url_path:
                    # 自动补全 /v1/models 路径
                    # 处理各种常见的 base URL 格式
                    if base.endswith("/v1"):
                        url = f"{base}/models"
                    elif base.endswith("/v1/"):
                        url = f"{base}models"
                    else:
                        url = f"{base}/v1/models"
                else:
                    # 用户已提供完整 URL，直接使用
                    if base.endswith("/models"):
                        url = base
                    else:
                        url = f"{base}/models"

                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }

            # 发送请求获取模型列表
            resp = requests.get(url, headers=headers, timeout=15, **proxy_kwargs)

            # 如果 401 错误，尝试不带 Bearer 前缀的认证方式
            if resp.status_code == 401 and provider_type not in ("gemini_native", "anthropic"):
                headers_alt = {
                    "Authorization": api_key,
                    "Content-Type": "application/json"
                }
                resp_alt = requests.get(
                    url, headers=headers_alt, timeout=15, **proxy_kwargs
                )
                if resp_alt.status_code == 200:
                    resp = resp_alt
                    headers = headers_alt  # 更新 headers 用于调试显示

            # 记录调试信息
            auth_value = headers.get("Authorization", "") or headers.get("x-api-key", "")
            debug_info = {
                "url": url,
                "auth_header": auth_value[:30] + "..." if len(auth_value) > 30 else auth_value,
                "status_code": resp.status_code
            }

            resp.raise_for_status()
            result = resp.json()

            # 解析模型列表
            models = []

            if provider_type == "gemini_native":
                # Google Gemini 响应格式
                for model in result.get("models", []):
                    model_name = model.get("name", "").replace("models/", "")
                    if model_name:
                        models.append({
                            "id": model_name,
                            "name": model.get("displayName", model_name),
                            "description": model.get("description", "")
                        })
            else:
                # OpenAI 兼容格式
                for model in result.get("data", []):
                    model_id = model.get("id", "")
                    if model_id:
                        models.append({
                            "id": model_id,
                            "name": model.get("id", model_id),
                            "owned_by": model.get("owned_by", "")
                        })

            # 按名称排序
            models.sort(key=lambda x: x.get("name", x.get("id", "")))

            return jsonify({
                "success": True,
                "message": f"成功获取 {len(models)} 个模型",
                "models": models,
                "debug_url": url,
                "debug_auth": debug_info.get("auth_header", "")
            })

        except requests.exceptions.Timeout:
            return jsonify({"success": False, "message": "获取模型列表超时，请检查网络连接", "models": []})
        except requests.exceptions.ConnectionError:
            return jsonify({"success": False, "message": "连接失败，请检查 Base URL 是否正确", "models": []})
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP 错误: {e.response.status_code}"
            debug_auth = debug_info.get("auth_header", "") if 'debug_info' in locals() else ""
            try:
                error_data = e.response.json()
                if "error" in error_data:
                    error_msg += f" - {error_data['error'].get('message', '')}"
                # 记录完整的错误信息用于调试
                import logging
                logging.warning(f"Fetch models failed: {url} -> {e.response.status_code}: {error_data}")
            except Exception:
                pass
            return jsonify({
                "success": False,
                "message": error_msg,
                "models": [],
                "debug_url": url if 'url' in locals() else "",
                "debug_auth": debug_auth
            })
        except Exception as e:
            return jsonify({"success": False, "message": f"获取失败: {str(e)}", "models": []})

    @app.route("/api/ai-models/<model_id>/test", methods=["POST"])
    def test_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue

            provider_type = model.get(
                "provider_type", model.get("provider", "openai_compatible")
            )
            api_key = resolve_runtime_api_key(model.get("api_key", ""), provider_type)
            base_url = model.get("base_url", "")
            append_base_url_path = model.get("append_base_url_path", True)
            model_name = model.get("model", "")
            purpose = model.get("purpose", "chat")
            oauth_account_id = model.get("oauth_account_id", "")
            proxy_url = model.get("proxy_url", "")

            if not api_key:
                return jsonify({"success": False, "message": "API Key is required"})
            if not base_url:
                return jsonify({"success": False, "message": "Base URL is required"})
            if not model_name:
                return jsonify({"success": False, "message": "Model is required"})

            try:
                import time

                import requests

                start_time = time.time()
                oauth_extra_headers = {}
                oauth_remove_headers = set()
                if oauth_account_id:
                    from nbot.core.oauth import get_oauth_manager

                    credential = get_oauth_manager().resolve_credential(oauth_account_id)
                    api_key = credential.access_token
                    oauth_extra_headers = dict(credential.extra_headers)
                    oauth_remove_headers = set(credential.remove_headers)
                # TTS 模型使用适配器测试
                if purpose == "tts":
                    from nbot.services.tts_adapters import get_adapter

                    tts_provider = model.get("tts_provider", "openai")
                    adapter = get_adapter(tts_provider)

                    test_config = {
                        "api_key": api_key,
                        "base_url": base_url,
                        "tts_provider": tts_provider,
                        "tts_url": model.get("tts_url", ""),
                        "tts_model": model.get("tts_model") or model_name,
                        "tts_voice": (model.get("tts_voice") or "alloy").strip(),
                        "tts_format": model.get("tts_format", "mp3"),
                        "tts_ref_audio": model.get("tts_ref_audio", ""),
                        "tts_user": model.get("tts_user", ""),
                        "proxy_url": proxy_url,
                    }

                    import tempfile
                    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
                    os.close(fd)

                    try:
                        adapter.synthesize("Hello", test_config, tmp_path)
                        elapsed_ms = round((time.time() - start_time) * 1000)
                        return jsonify({"success": True, "message": "TTS connection successful", "elapsed_ms": elapsed_ms})
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                # STT 模型使用适配器测试
                elif purpose == "stt":
                    from nbot.services.stt_adapters import get_adapter
                    from nbot.services.tts_config import normalize_stt_config

                    stt_config = normalize_stt_config(model)
                    stt_provider = stt_config.get("stt_provider") or stt_config.get("provider_type", "")
                    adapter = get_adapter(stt_provider)

                    # 生成一个最小的静音 WAV 文件用于测试连接
                    import struct
                    import tempfile

                    sample_rate = 16000
                    duration_ms = 100
                    num_samples = sample_rate * duration_ms // 1000
                    # WAV header + 16-bit PCM silence
                    wav_data = b"RIFF"
                    wav_data += struct.pack("<I", 36 + num_samples * 2)
                    wav_data += b"WAVE"
                    wav_data += b"fmt "
                    wav_data += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
                    wav_data += b"data"
                    wav_data += struct.pack("<I", num_samples * 2)
                    wav_data += b"\x00" * (num_samples * 2)

                    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)

                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(wav_data)
                        adapter.transcribe(tmp_path, stt_config, "zh")
                        elapsed_ms = round((time.time() - start_time) * 1000)
                        return jsonify({"success": True, "message": "STT connection successful", "elapsed_ms": elapsed_ms})
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                # 图片生成模型使用不同的测试方式
                elif purpose == "image_generation":
                    # 直接使用用户输入的完整URL
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    # 发送一个简单的测试请求（不实际生成图片）
                    # 只检查API是否可访问
                    payload = {
                        "model": model_name,
                        "prompt": "test",
                        "n": 1,
                        "size": "1024x1024"
                    }
                    resp = requests.post(
                        base_url,
                        json=payload,
                        headers=headers,
                        timeout=30,
                        **model_proxy_request_kwargs(proxy_url),
                    )
                    # 对于图片生成API，即使返回400（参数错误）也说明连接成功
                    elapsed_ms = round((time.time() - start_time) * 1000)
                    if resp.status_code in [200, 400, 401]:
                        return jsonify({"success": True, "message": "Connection successful", "elapsed_ms": elapsed_ms})
                    resp.raise_for_status()
                    return jsonify({"success": True, "message": "Connection successful", "elapsed_ms": elapsed_ms})
                else:
                    # 其他模型使用协议适配器测试
                    from nbot.core.protocols import get_protocol

                    protocol = get_protocol(provider_type)
                    url = protocol.resolve_url(
                        base_url,
                        model=model_name,
                        append_base_url_path=append_base_url_path,
                        api_key=api_key,
                    )
                    requires_stream = (
                        "chatgpt.com/backend-api/codex" in base_url
                    )
                    headers = protocol.build_headers(api_key, stream=requires_stream)
                    headers.update(oauth_extra_headers)
                    for header_name in oauth_remove_headers:
                        headers.pop(header_name, None)
                    payload = protocol.build_payload(
                        model_name,
                        [{"role": "user", "content": "Hello"}],
                        stream=requires_stream,
                        max_tokens=10,
                        base_url=base_url,
                        provider_type=provider_type,
                    )
                    resp = requests.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=30,
                        **model_proxy_request_kwargs(proxy_url),
                    )
                    resp.raise_for_status()
                    elapsed_ms = round((time.time() - start_time) * 1000)
                    return jsonify({"success": True, "message": "Connection successful", "elapsed_ms": elapsed_ms})
            except requests.exceptions.Timeout:
                return jsonify({"success": False, "message": "Connection timed out"})
            except requests.exceptions.ConnectionError:
                return jsonify({"success": False, "message": "Connection failed"})
            except requests.exceptions.HTTPError as e:
                error_msg = f"HTTP error: {e.response.status_code}"
                try:
                    error_data = e.response.json()
                    if "error" in error_data:
                        error_msg += (
                            f" - {error_data['error'].get('message', 'Unknown error')}"
                        )
                except Exception:
                    pass
                return jsonify({"success": False, "message": error_msg})
            except Exception as e:
                return jsonify({"success": False, "message": f"Test failed: {str(e)}"})

        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>/probe", methods=["POST"])
    def probe_ai_model(model_id):
        """实测模型能力（chat / tools / vision / stream），结果写回配置。

        与 ``/test`` 的区别：``/test`` 只发一次最小请求验证连通性，
        本接口会分别验证工具调用、视觉输入与流式能力，供"能力位"落地。
        """
        from nbot.web.model_probe import apply_probe_result, probe_model_config

        model = next((m for m in server.ai_models if m.get("id") == model_id), None)
        if not model:
            return jsonify({"success": False, "error": "Model not found"}), 404

        config = dict(model)
        provider_type = config.get("provider_type", config.get("provider", ""))
        config["api_key"] = resolve_runtime_api_key(config.get("api_key", ""), provider_type)

        result = probe_model_config(config, timeout=float(request.args.get("timeout") or 20))
        if result.get("supported"):
            apply_probe_result(model, result)
            try:
                server._save_data("ai_models")
            except Exception as exc:
                _log.warning("保存实测能力失败: %s", exc)

        return jsonify({
            "success": bool(result.get("ok")),
            "result": result,
            "capabilities": result.get("capabilities", {}),
            "probed_at": model.get("capabilities_probed_at", ""),
        })

    # ========== 故障转移队列 API ==========

    @app.route("/api/ai-models/failover-status")
    def get_failover_status():
        """获取故障转移队列状态（各模型健康信息）"""
        from nbot.core.failover import get_failover_state

        state = get_failover_state()
        return jsonify({
            "success": True,
            "health": state.get_all_health_summary(),
        })

    @app.route("/api/ai-models/failover-reset", methods=["POST"])
    def reset_failover():
        """重置故障转移状态（清除冷却）"""
        from nbot.core.failover import get_failover_state

        data = request.json or {}
        model_id = data.get("model_id")
        get_failover_state().reset(model_id)
        return jsonify({"success": True})

    @app.route("/api/ai-models/failover-queue/<purpose>")
    def get_failover_queue(purpose):
        """获取指定用途的故障转移队列"""
        if purpose not in MODEL_PURPOSES:
            return jsonify({"error": "Invalid purpose"}), 400

        from nbot.core.failover import get_failover_state
        from nbot.web.utils.config_loader import get_model_configs_by_purpose

        configs = get_model_configs_by_purpose(purpose)
        state = get_failover_state()
        health = state.get_all_health_summary()

        queue = []
        for cfg in configs:
            mid = cfg.get("model_id", "")
            model_name = cfg.get("model", "")
            token_usage = {}
            try:
                from nbot.core.token_stats import get_token_stats_manager
                token_usage = get_token_stats_manager().get_model_usage(model_name)
            except Exception:
                pass
            queue.append({
                "model_id": mid,
                "name": cfg.get("name", ""),
                "model": model_name,
                "provider": cfg.get("provider", ""),
                "priority": cfg.get("priority", 0),
                "health": health.get(mid, {}),
                "token_limit_daily": cfg.get("token_limit_daily", 0) or 0,
                "token_limit_weekly": cfg.get("token_limit_weekly", 0) or 0,
                "failover_timeout": cfg.get("failover_timeout", 0) or 0,
                "token_usage": token_usage,
            })

        return jsonify({
            "success": True,
            "purpose": purpose,
            "queue": queue,
        })

    @app.route("/api/ai-models/failover-reorder", methods=["POST"])
    def reorder_failover_queue():
        """批量更新模型优先级并自动应用P0模型。

        请求体: { "purpose": "chat", "priorities": [{"id": "xxx", "priority": 0}, ...] }
        """
        data = request.json or {}
        purpose = data.get("purpose", "chat")
        priorities = data.get("priorities", [])

        if not priorities:
            return jsonify({"error": "priorities is required"}), 400

        # Build a lookup of model_id -> new priority
        priority_map = {p["id"]: p["priority"] for p in priorities if "id" in p}

        updated = []
        for model in server.ai_models:
            if model["id"] in priority_map:
                model["priority"] = priority_map[model["id"]]
                updated.append(model["id"])

        if updated:
            server._save_data("ai_models")

        # Auto-apply the P0 model for this purpose
        p0_model = None
        for model in server.ai_models:
            if model["id"] in priority_map and priority_map[model["id"]] == 0:
                if model.get("purpose", "chat") == purpose:
                    p0_model = model
                    break

        if p0_model:
            server._apply_ai_model(p0_model["id"], purpose=purpose)

        return jsonify({
            "success": True,
            "updated": len(updated),
            "p0_model_id": p0_model["id"] if p0_model else None,
        })

    @app.route("/api/ai-models/failover-detail/<model_id>")
    def get_failover_model_detail(model_id):
        """获取故障转移队列中单个模型的详情"""
        from nbot.core.failover import get_failover_state

        model = next((m for m in server.ai_models if m["id"] == model_id), None)
        if not model:
            return jsonify({"error": "Model not found"}), 404

        state = get_failover_state()
        health = state.get_all_health_summary().get(model_id, {})
        model_name = model.get("model", "")

        token_usage = {}
        try:
            from nbot.core.token_stats import get_token_stats_manager
            token_usage = get_token_stats_manager().get_model_usage(model_name)
        except Exception:
            pass

        return jsonify({
            "success": True,
            "model_id": model_id,
            "name": model.get("name", ""),
            "model": model_name,
            "provider": model.get("provider", ""),
            "purpose": model.get("purpose", "chat"),
            "priority": model.get("priority", 0),
            "enabled": model.get("enabled", True),
            "health": health,
            "token_limit_daily": model.get("token_limit_daily", 0) or 0,
            "token_limit_weekly": model.get("token_limit_weekly", 0) or 0,
            "failover_timeout": model.get("failover_timeout", 0) or 0,
            "token_usage": token_usage,
            "input_price": model.get("input_price"),
            "output_price": model.get("output_price"),
            "cached_input_price": model.get("cached_input_price"),
            "max_tokens": model.get(
                "max_tokens", default_max_tokens_for(model.get("purpose", "chat"))
            ),
            "temperature": model.get("temperature", 0.7),
        })

    @app.route("/api/ai-models/failover-token-limit", methods=["POST"])
    def set_failover_token_limit():
        """设置模型的 token 限额"""
        data = request.json or {}
        model_id = data.get("model_id")
        if not model_id:
            return jsonify({"error": "model_id is required"}), 400

        model = next((m for m in server.ai_models if m["id"] == model_id), None)
        if not model:
            return jsonify({"error": "Model not found"}), 404

        if "token_limit_daily" in data:
            model["token_limit_daily"] = max(0, int(data["token_limit_daily"] or 0))
        if "token_limit_weekly" in data:
            model["token_limit_weekly"] = max(0, int(data["token_limit_weekly"] or 0))
        if "failover_timeout" in data:
            model["failover_timeout"] = max(0, int(data["failover_timeout"] or 0))

        server._save_data("ai_models")
        return jsonify({"success": True})
