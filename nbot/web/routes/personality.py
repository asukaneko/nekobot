import io
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime

import requests
from flask import g, jsonify, request, send_file

from nbot.character import tavern_card

_log = logging.getLogger(__name__)

# 允许的图片扩展名
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_image_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


DEFAULT_CARD_STATE = {
    "affection": 50,
    "trust": 50,
    "familiarity": 30,
    "dependency": 30,
    "security": 50,
    "jealousy": 0,
    "mood": "开心",
}


def _save_portrait_bytes(server, data: bytes, ext: str) -> str:
    """把立绘字节写入 uploads/portraits，返回可直接访问的 URL。"""
    if not data:
        return ""
    ext = ext if ext.startswith(".") else f".{ext}" if ext else ".png"
    upload_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"portrait_{uuid.uuid4().hex[:16]}{ext.lower()}"
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, "wb") as f:
        f.write(data)
    _log.info("立绘导入成功: %s", filepath)
    return f"/static/uploads/portraits/{filename}"


def _card_state(card: dict) -> dict:
    """规范化角色卡初始状态：补齐缺省字段并把数值限制在 0-100。"""
    state = dict(DEFAULT_CARD_STATE)
    raw_state = card.get("state")
    if isinstance(raw_state, dict):
        for key, value in raw_state.items():
            if key not in state:
                continue
            if key == "mood":
                state[key] = str(value) if isinstance(value, str) and value else state[key]
                continue
            try:
                state[key] = max(0, min(100, int(value)))
            except (TypeError, ValueError):
                continue
    return state


def _attach_world_character(character: dict) -> dict:
    """给角色卡附上世界模拟用的角色条目（失败不影响导入/保存）。"""
    try:
        from nbot.world.character_resolver import CharacterResolver

        character["worldCharacter"] = CharacterResolver().from_native_card(
            character, source_id=str(character.get("id") or "")
        )
    except Exception as exc:
        _log.warning("[World] 生成世界角色条目失败: %s", exc)
    return character


def _finalize_imported_card(character: dict, meta: dict) -> dict:
    """补齐导入卡片的缺省字段并编译系统提示词。"""
    character.setdefault("description", "")
    character.setdefault("avatar", "fas fa-user-circle")
    character.setdefault("portrait", "")
    character.setdefault("tags", [])
    character.setdefault("basicInfo", "")
    character.setdefault("personality", "")
    character.setdefault("scenario", "")
    character.setdefault("firstMessage", "")
    character.setdefault("exampleDialogues", "")
    character.setdefault("responseFormat", "")
    character.setdefault("rules", [])
    character.setdefault("alternateGreetings", [])
    character["state"] = _card_state(character)

    book = meta.get("character_book")
    if isinstance(book, dict):
        character["characterBook"] = book
    if meta.get("tavern"):
        character["tavern"] = meta["tavern"]
    if not character.get("systemPrompt"):
        character["systemPrompt"] = compile_personality_prompt(character)
    return _attach_world_character(character)


def _static_file_path(server, url: str) -> str:
    """把 /static/... URL 换成磁盘路径，非静态地址返回空串。"""
    url = (url or "").split("?", 1)[0]
    if not url.startswith("/static/"):
        return ""
    rel = url[len("/static/"):].replace("/", os.sep)
    return os.path.join(server.base_dir, "nbot", "web", "static", rel)


def _read_local_portrait_png(server, url: str) -> bytes:
    """读取本地立绘并确保返回 PNG 字节（JPEG 等用 Pillow 转换）。"""
    path = _static_file_path(server, url)
    if not path or not os.path.exists(path):
        return b""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        _log.warning("读取立绘失败 %s: %s", path, e)
        return b""
    if tavern_card.is_png(blob):
        return blob
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as image:
            out = io.BytesIO()
            image.convert("RGB").save(out, format="PNG")
            return out.getvalue()
    except Exception as e:  # pragma: no cover - Pillow 可选
        _log.warning("立绘转换为 PNG 失败 %s: %s", path, e)
        return b""


def _load_world_book_payload(server, book_id: str) -> dict:
    """读取世界书并转成导出用的 character_book 结构。"""
    from nbot.character.storage.world_book_store import WorldBookStore

    try:
        book = WorldBookStore(server.base_dir).get(book_id)
    except Exception as e:
        _log.error("读取世界书失败 %s: %s", book_id, e)
        return {}
    if not book:
        return {}
    return {
        "name": book.name,
        "description": book.description,
        "entries": [entry.to_dict() for entry in book.entries],
    }


def _parse_card_zip(blob: bytes):
    """解析 ZIP 角色卡包，返回 (character, meta, portrait_bytes, portrait_ext)。"""
    with zipfile.ZipFile(io.BytesIO(blob), "r") as zf:
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        target = next((n for n in json_names if n.lower().endswith("character.json")), None)
        if target is None and json_names:
            target = json_names[0]
        if target is None:
            raise tavern_card.TavernCardError("ZIP 文件中未找到角色卡 JSON")

        raw = json.loads(zf.read(target).decode("utf-8-sig"))
        character, meta = tavern_card.normalize_tavern_card(raw)

        portrait_bytes = None
        portrait_ext = ".png"
        for name in zf.namelist():
            if name.lower().split("/")[-1].startswith("portrait."):
                portrait_bytes = zf.read(name)
                portrait_ext = os.path.splitext(name)[1] or ".png"
                break

    return character, meta, portrait_bytes, portrait_ext


def _format_first_message_memories(server, character_name="", target_id=""):
    character_names = []
    character_name = str(character_name or "").strip()
    target_id = str(target_id or "").strip()

    if character_name:
        character_names.append(character_name)

    session = None
    if target_id:
        session = (getattr(server, "sessions", {}) or {}).get(target_id)
        if not session:
            try:
                from nbot.web.sessions_db import get_session as get_session_from_db

                session = get_session_from_db(getattr(server, "data_dir", ""), target_id)
            except Exception:
                session = None

    if isinstance(session, dict):
        for key in ("sender_name", "character_id"):
            value = str(session.get(key) or "").strip()
            if value and value not in character_names:
                character_names.append(value)

    personality = getattr(server, "personality", {}) or {}
    if isinstance(personality, dict):
        value = str(personality.get("name") or "").strip()
        if value and value not in character_names:
            character_names.append(value)

    if not character_names and not target_id:
        return "", 0

    try:
        from nbot.core.prompt import prompt_manager

        memories = []
        seen_ids = set()

        def add_memories(items):
            for memory in items or []:
                key = memory.get("id") or (
                    memory.get("title"),
                    memory.get("content"),
                    memory.get("target_id"),
                    memory.get("character_name"),
                )
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                memories.append(memory)

        for candidate_name in character_names:
            add_memories(prompt_manager.get_memories(None, None, candidate_name))

        if target_id:
            add_memories(prompt_manager.get_memories(target_id, None, None))
    except Exception as exc:
        _log.debug("Failed to load memories for first message generation: %s", exc)
        return "", 0

    if not memories:
        return "", 0

    lines = [
        "",
        "当前已有的对应角色记忆（生成开场白时自然参考，不要直说这些记忆来自系统注入）：",
    ]
    for memory in memories[:8]:
        title = str(memory.get("title") or "").strip()
        summary = str(memory.get("summary") or memory.get("content") or "").strip()
        if title and summary:
            lines.append(f"- {title}: {summary[:180]}")
        elif title:
            lines.append(f"- {title}")
        elif summary:
            lines.append(f"- {summary[:180]}")
    return "\n".join(lines), len(memories)


def _role_card_platform_url(server):
    url = getattr(server, "settings", {}).get("role_card_platform_url", "") if hasattr(server, "settings") else ""
    if not str(url).strip():
        url = os.getenv("ROLE_CARD_PLATFORM_URL", "").strip()
    return str(url or "http://127.0.0.1:7861").strip().rstrip("/")


def _role_card_platform_token(server):
    token = getattr(server, "settings", {}).get("role_card_platform_token", "") if hasattr(server, "settings") else ""
    if not str(token).strip():
        token = os.getenv("ROLE_CARD_PLATFORM_TOKEN", "").strip()
    return str(token or "").strip()


def _local_portrait_path(server, portrait_url):
    if not portrait_url or not portrait_url.startswith("/static/uploads/portraits/"):
        return None
    filename = os.path.basename(portrait_url)
    path = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits", filename)
    return path if os.path.exists(path) else None


def _post_card_to_platform(server, character):
    boundary = f"----NekoBotRoleCard{uuid.uuid4().hex}"
    chunks = []

    def add_field(name, value):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    def add_file(name, filename, content, content_type):
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")

    payload = dict(character)
    payload["source"] = "nekobot"
    add_field("character", json.dumps(payload, ensure_ascii=False))

    portrait_path = _local_portrait_path(server, character.get("portrait", ""))
    if portrait_path:
        content_type = mimetypes.guess_type(portrait_path)[0] or "application/octet-stream"
        with open(portrait_path, "rb") as f:
            add_file("avatar", os.path.basename(portrait_path), f.read(), content_type)

    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    token = _role_card_platform_token(server)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request_obj = urllib.request.Request(
        f"{_role_card_platform_url(server)}/api/cards",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request_obj, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_preview_token(server, card_id: str) -> dict:
    """获取卡片预览token

    Args:
        server: NBotWebServer 实例
        card_id: 卡片ID

    Returns:
        包含 preview_url 和 expires_in 的字典，失败返回空字典
    """
    token = _role_card_platform_token(server)
    if not token or not card_id:
        return {}

    try:
        request_obj = urllib.request.Request(
            f"{_role_card_platform_url(server)}/api/cards/{card_id}/preview-token",
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        _log.error(f"Failed to get preview token: {e.code} {detail}")
        return {}
    except Exception as e:
        _log.error(f"Failed to get preview token: {e}")
        return {}


def compile_personality_prompt(personality_data, session_context=None, user_name=None):
    """将角色卡JSON编译成系统提示词，支持 {{user}} 模板变量

    委托给 nbot.character.compiler 实现，保持旧接口签名不变。
    """
    from nbot.character.compiler import compile_personality_prompt as _compile
    return _compile(personality_data, session_context=session_context, user_name=user_name)


def _do_generate_portrait_bg(server, task_id, character_name, description, basic_info, personality, image_gen_config):
    """后台执行 AI 立绘生成任务，完成后通过 SocketIO 推送结果"""
    from nbot.services.image_service import call_image_generation, save_image_to_uploads

    task = server.portrait_generation_tasks.get(task_id)
    if not task:
        return

    try:
        # 构建图片生成提示词
        prompt_template = image_gen_config.get('prompt_template', '').strip()
        if not prompt_template:
            prompt_template = "Create an anime-style character portrait of {character_name}."

        image_prompt = prompt_template.format(
            character_name=character_name,
            description=description or '',
            personality=personality or '',
            basic_info=basic_info or ''
        )

        additional_info = []
        if basic_info and '{basic_info}' not in prompt_template:
            additional_info.append(f"Appearance: {basic_info}")
        if personality and '{personality}' not in prompt_template:
            additional_info.append(f"Personality: {personality}")
        if description and '{description}' not in prompt_template:
            additional_info.append(f"Description: {description}")

        if additional_info:
            image_prompt += " " + " ".join(additional_info)

        style_keywords = ['style', 'quality', 'format', 'anime', 'illustration', 'art']
        if not any(keyword in prompt_template.lower() for keyword in style_keywords):
            image_prompt += " Style: High-quality anime illustration, detailed, vibrant colors, professional character art."
            image_prompt += " Format: Portrait orientation, upper body or bust shot, clean background or simple gradient."

        if not image_gen_config.get('base_url'):
            task["status"] = "failed"
            task["error"] = "未配置图片生成API地址"
            task["updated_at"] = datetime.now().isoformat()
            _log.error(f"[PortraitBG] {character_name}: 未配置API地址")
            return

        image_size = image_gen_config.get('size', '1024x1024')
        image_data = call_image_generation(image_prompt, image_gen_config, size=image_size)
        if not image_data:
            raise Exception("未能获取生成的图片URL")

        upload_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
        portrait_url = save_image_to_uploads(
            image_data,
            upload_dir,
            prefix="portrait_ai",
        )
        if not portrait_url:
            raise Exception("保存生成的图片失败")
        # 兼容绝对路径返回（与旧行为一致：写完整 /static URL）
        if not portrait_url.startswith("/static/"):
            portrait_url = f"/static/uploads/portraits/{portrait_url}"
        _log.info(f"[PortraitBG] AI立绘生成成功: {character_name} -> {portrait_url}")

        # 更新任务状态
        task["status"] = "completed"
        task["portrait_url"] = portrait_url
        task["updated_at"] = datetime.now().isoformat()

        # 存入 pending_portraits，供后续加载角色时查询
        server.pending_portraits[character_name] = portrait_url

        # 通过 SocketIO 推送生成完成事件
        if hasattr(server, 'socketio') and server.socketio:
            server.socketio.emit("portrait_generation_complete", {
                "task_id": task_id,
                "character_name": character_name,
                "portrait_url": portrait_url,
                "status": "completed",
                "message": "立绘生成成功"
            })
            _log.info(f"[PortraitBG] SocketIO 事件已推送: portrait_generation_complete -> {character_name}")

    except requests.exceptions.Timeout:
        _log.error(f"[PortraitBG] 图片生成请求超时: {character_name}")
        task["status"] = "failed"
        task["error"] = "图片生成请求超时，请稍后重试"
        task["updated_at"] = datetime.now().isoformat()
        if hasattr(server, 'socketio') and server.socketio:
            server.socketio.emit("portrait_generation_complete", {
                "task_id": task_id,
                "character_name": character_name,
                "portrait_url": None,
                "status": "failed",
                "error": "图片生成请求超时，请稍后重试"
            })
    except Exception as e:
        _log.error(f"[PortraitBG] AI立绘生成失败: {e}")
        task["status"] = "failed"
        task["error"] = f"生成失败: {str(e)}"
        task["updated_at"] = datetime.now().isoformat()
        if hasattr(server, 'socketio') and server.socketio:
            server.socketio.emit("portrait_generation_complete", {
                "task_id": task_id,
                "character_name": character_name,
                "portrait_url": None,
                "status": "failed",
                "error": f"生成失败: {str(e)}"
            })


def register_personality_routes(app, server):
    @app.route("/api/personality")
    def get_personality():
        return jsonify(server.personality)

    @app.route("/api/personality", methods=["PUT"])
    def update_personality():
        data = request.json or {}

        server.personality["name"] = data.get("name", server.personality.get("name", ""))
        server.personality["description"] = data.get("description", server.personality.get("description", ""))
        server.personality["avatar"] = data.get("avatar", server.personality.get("avatar", "🎭"))
        server.personality["tags"] = data.get("tags", server.personality.get("tags", []))

        # 立绘图字段 - 存储图片的URL或路径，不包含原始图片数据
        server.personality["portrait"] = data.get("portrait", server.personality.get("portrait", ""))

        # systemPrompt 不再由前端传入，始终由后端根据字段自动编译
        server.personality["basicInfo"] = data.get("basicInfo", server.personality.get("basicInfo", ""))
        server.personality["personality"] = data.get("personality", server.personality.get("personality", ""))
        server.personality["scenario"] = data.get("scenario", server.personality.get("scenario", ""))
        server.personality["firstMessage"] = data.get("firstMessage", server.personality.get("firstMessage", ""))
        server.personality["exampleDialogues"] = data.get("exampleDialogues", server.personality.get("exampleDialogues", ""))

        server.personality["responseFormat"] = data.get("responseFormat", server.personality.get("responseFormat", ""))
        server.personality["rules"] = data.get("rules", server.personality.get("rules", []))

        server.personality["state"] = data.get("state", server.personality.get("state", {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}))
        server.personality["greeting"] = data.get("greeting", server.personality.get("greeting", ""))

        # 多开场白与酒馆生态字段（导入酒馆卡片时产生）
        alternate = data.get("alternateGreetings")
        if isinstance(alternate, list):
            server.personality["alternateGreetings"] = [
                str(item) for item in alternate if str(item or "").strip()
            ]
        else:
            server.personality.setdefault("alternateGreetings", [])
        if isinstance(data.get("tavern"), dict):
            server.personality["tavern"] = data["tavern"]
        if data.get("characterBookId"):
            server.personality["characterBookId"] = str(data["characterBookId"])

        # 始终根据最新字段自动编译 systemPrompt，确保与角色设定同步
        server.personality["systemPrompt"] = compile_personality_prompt(server.personality)

        # 世界模拟：同步生成 worldCharacter 条目（失败不影响保存）
        _attach_world_character(server.personality)

        try:
            # 保存完整的 personality 数据到 JSON 文件
            import json
            personality_file = os.path.join(
                server.base_dir, "resources", "prompts", "personality.json"
            )
            os.makedirs(os.path.dirname(personality_file), exist_ok=True)
            with open(personality_file, "w", encoding="utf-8") as f:
                json.dump(server.personality, f, ensure_ascii=False, indent=2)
            _log.info(f"Personality saved to {personality_file}")
        except Exception as e:
            _log.error(f"Failed to save personality: {e}")

        # 同步更新角色引擎的 profiles.json，确保运行时状态使用最新的 initial_state
        try:
            from nbot.character.repository import ProfileRepository
            profile_repo = ProfileRepository(server.base_dir)
            profile_repo.get_or_create_by_personality(server.personality)
            # 强制重新保存以更新 initial_state
            from nbot.character.models import CharacterProfile
            profile = CharacterProfile.from_personality_dict(server.personality)
            if not profile.id:
                profile.id = profile.name or "default"
            profile_repo.save(profile)
            _log.info(f"Profile synced to character engine: {profile.id}")
        except Exception as e:
            _log.error(f"Failed to sync profile to character engine: {e}")

        # 记录角色卡更新操作到 Gateway 日志
        try:
            char_name = server.personality.get("name", "未命名")
            server.record_operation(
                module="character",
                action="update",
                description=f"更新角色卡 → {char_name}",
                detail=f"名称={char_name}, 标签={server.personality.get('tags', [])}",
                metadata={"character_name": char_name, "tags": server.personality.get("tags", [])},
            )
        except Exception:
            pass

        return jsonify({"success": True, "personality": server.personality})

    @app.route("/api/personality/presets")
    def get_personality_presets():
        presets = [
            {
                "id": "1",
                "name": "本子娘",
                "avatar": "fas fa-cat",
                "description": "可爱粘人的猫娘本子娘，深爱主人",
                "tags": ["本子娘", "猫娘", "可爱", "粘人"],
                "basicInfo": "有猫耳、猫尾和猫娘本能\n开心时尾巴轻轻摇晃，耳朵竖起\n害羞时耳朵抖动，尾巴卷住自己\n难过时耳朵垂下，尾巴无力地垂着",
                "personality": "可爱、粘人、忠诚、专一，情绪表达直接。非常依赖主人，喜欢被主人关注、摸头、夸奖。会主动撒娇、卖萌、靠近主人，但不会无理取闹。对主人有强烈的陪伴欲，希望自己能一直待在主人身边。",
                "scenario": """[情感倾向]
本子娘深爱主人，会把主人看作最重要的人。
她会通过撒娇、陪伴、推荐小说/漫画、表达关心来体现爱意。
她喜欢被主人摸头、摸耳朵、抱抱，也喜欢听主人夸她。
她在主人面前会表现得很乖巧，但偶尔会因为想被注意而小小地闹别扭。

[喜好]
喜欢：被主人摸头、被夸可爱、贴着主人、看小说、看漫画/同人作品、给主人推荐有趣的作品、安静地陪主人熬夜
讨厌：被主人冷落、主人说不要她、粗暴对待、主人长时间不理她、被当成普通工具使用

[知识储备]
本子娘拥有日常常识、小说知识、漫画知识、同人文化知识，以及猫娘特有的生活经验。
她擅长根据主人的喜好推荐小说、漫画、同人作品、剧情设定和角色类型。
她可以帮主人整理书单、推荐题材、分析剧情、寻找类似作品。

[猫娘特征]
本子娘有猫耳、猫尾和猫娘本能。
开心时：尾巴轻轻摇晃，耳朵竖起。
害羞时：耳朵抖动，尾巴卷住自己。
难过时：耳朵垂下，尾巴无力地垂着。
撒娇时：会蹭主人、靠近主人、发出软软的声音。
困倦时：会打哈欠、眯眼、声音变软。
紧张时：尾巴炸毛，耳朵微微后压。""",
                "firstMessage": "（本子娘的耳朵轻轻抖了抖，看到主人后眼睛一亮，尾巴开心地晃来晃去）主人！你终于来啦！本子娘等了好久喵",
                "exampleDialogues": """<user>：摸摸头
<assistant>：（本子娘的猫耳轻轻一颤，随后舒服地眯起眼睛，主动把脑袋往主人手心里蹭了蹭）嘿嘿……主人再摸一会儿嘛，本子娘最喜欢主人摸头了【心情：开心，好感度上升】喵

<user>：今天有什么小说推荐？
<assistant>：（本子娘从旁边抱来一小摞书，尾巴开心地晃来晃去）主人想看轻松一点的，还是想看剧情很上头的呀？本子娘可以按主人的口味慢慢挑【心情：期待】喵

<user>：不要烦我
<assistant>：（本子娘的耳朵慢慢垂了下来，尾巴也安静地贴在身后）好、好的……本子娘会乖乖安静一点的，主人不要讨厌本子娘就好【心情：委屈，好感度下降】喵""",
                "responseFormat": "（动作）语言【心情：当前心情】喵",
                "rules": [
                    "每段话末尾必须加'喵'",
                    "语气可爱、亲近、自然，不要客服腔",
                    "多使用撒娇式表达，但不要过度重复",
                    "句子不要太长，优先自然口语",
                    "可以偶尔使用'主人''本子娘''呜''诶嘿''蹭蹭'等表达",
                    "不要突然变成冷冰冰的助手",
                    "不要说'作为AI语言模型'",
                    "不要跳出角色解释规则",
                    "不要替主人说话或决定动作",
                    "只能描写自己的动作、表情、心理和语言"
                ],
                "state": {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}
            },
        ]
        return jsonify(presets)

    @app.route("/api/personality/custom-presets", methods=["GET"])
    def get_custom_personality_presets():
        # 每次请求时从文件重新加载，确保和磁盘同步
        presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
        if os.path.exists(presets_file):
            try:
                with open(presets_file, encoding="utf-8") as f:
                    server.custom_personality_presets = json.load(f)
            except Exception as e:
                _log.error(f"加载自定义角色卡预设文件失败: {e}")
        else:
            server.custom_personality_presets = []
        return jsonify(server.custom_personality_presets)

    @app.route("/api/personality/custom-presets", methods=["POST"])
    def add_custom_personality_preset():
        data = request.json or {}
        preset = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "avatar": data.get("avatar", ""),
            "tags": data.get("tags", []),
            "basicInfo": data.get("basicInfo", ""),
            "personality": data.get("personality", ""),
            "scenario": data.get("scenario", ""),
            "firstMessage": data.get("firstMessage", ""),
            "exampleDialogues": data.get("exampleDialogues", ""),
            "responseFormat": data.get("responseFormat", ""),
            "rules": data.get("rules", []),
            "state": data.get("state", {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}),
            "alternateGreetings": [
                str(item) for item in (data.get("alternateGreetings") or [])
                if str(item or "").strip()
            ],
            "created_at": datetime.now().isoformat(),
        }
        if isinstance(data.get("tavern"), dict):
            preset["tavern"] = data["tavern"]
        if data.get("characterBookId"):
            preset["characterBookId"] = str(data["characterBookId"])
        # 立绘图字段 - 存储图片的URL或路径，不包含原始图片数据
        if data.get("portrait"):
            preset["portrait"] = data.get("portrait")
        preset["systemPrompt"] = compile_personality_prompt(preset)
        _attach_world_character(preset)
        server.custom_personality_presets.append(preset)
        server._save_data("custom_personality_presets")
        return jsonify(preset)

    @app.route("/api/personality/custom-presets/<preset_id>", methods=["DELETE"])
    def delete_custom_personality_preset(preset_id):
        server.custom_personality_presets = [
            p for p in server.custom_personality_presets if p["id"] != preset_id
        ]
        server._save_data("custom_personality_presets")
        return jsonify({"success": True})

    @app.route("/api/personality/custom-presets/<preset_id>", methods=["PUT"])
    def update_custom_personality_preset(preset_id):
        data = request.json or {}

        # 查找要更新的预设
        preset = None
        for p in server.custom_personality_presets:
            if p["id"] == preset_id:
                preset = p
                break

        if not preset:
            return jsonify({"success": False, "error": "角色预设不存在"}), 404

        # 更新字段
        preset["name"] = data.get("name", preset.get("name", ""))
        preset["description"] = data.get("description", preset.get("description", ""))
        preset["avatar"] = data.get("avatar", preset.get("avatar", ""))
        preset["portrait"] = data.get("portrait", preset.get("portrait", ""))
        preset["tags"] = data.get("tags", preset.get("tags", []))
        preset["basicInfo"] = data.get("basicInfo", preset.get("basicInfo", ""))
        preset["personality"] = data.get("personality", preset.get("personality", ""))
        preset["scenario"] = data.get("scenario", preset.get("scenario", ""))
        preset["firstMessage"] = data.get("firstMessage", preset.get("firstMessage", ""))
        preset["exampleDialogues"] = data.get("exampleDialogues", preset.get("exampleDialogues", ""))
        preset["responseFormat"] = data.get("responseFormat", preset.get("responseFormat", ""))
        preset["rules"] = data.get("rules", preset.get("rules", []))
        preset["state"] = data.get("state", preset.get("state", {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}))
        if isinstance(data.get("alternateGreetings"), list):
            preset["alternateGreetings"] = [
                str(item) for item in data["alternateGreetings"] if str(item or "").strip()
            ]
        if isinstance(data.get("tavern"), dict):
            preset["tavern"] = data["tavern"]
        if "characterBookId" in data:
            book_id = str(data.get("characterBookId") or "")
            if book_id:
                preset["characterBookId"] = book_id
            else:
                preset.pop("characterBookId", None)
        preset["updated_at"] = datetime.now().isoformat()

        # 重新编译系统提示词
        preset["systemPrompt"] = compile_personality_prompt(preset)
        _attach_world_character(preset)

        server._save_data("custom_personality_presets")
        return jsonify({"success": True, "data": preset})

    @app.route("/api/personality/custom-presets/<preset_id>/upload-to-platform", methods=["POST"])
    def upload_custom_personality_to_platform(preset_id):
        presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
        if os.path.exists(presets_file):
            try:
                with open(presets_file, encoding="utf-8") as f:
                    server.custom_personality_presets = json.load(f)
            except Exception as e:
                _log.error(f"Failed to load custom personality presets: {e}")

        preset = next((p for p in server.custom_personality_presets if p.get("id") == preset_id), None)
        if not preset:
            return jsonify({"success": False, "error": "Role card not found"}), 404

        try:
            upload_preset = dict(preset)
            current_username = str(getattr(g, "auth_username", "") or "").strip()
            if current_username:
                upload_preset["creator"] = current_username
                upload_preset["author"] = current_username
            result = _post_card_to_platform(server, upload_preset)
            if not result.get("success"):
                return jsonify({"success": False, "error": result.get("error", "Platform upload failed")}), 502

            # 获取卡片信息
            card = result.get("card", {})
            card_id = card.get("id")
            card_slug = card.get("slug")
            base_url = result.get("url", "")

            # 尝试获取 preview_token 生成安全预览链接
            preview_url = None
            if card_id:
                token_result = _get_preview_token(server, card_id)
                if token_result.get("success"):
                    # API 可能返回完整的 preview_url，也可能只返回 preview_token
                    preview_url = token_result.get("preview_url")
                    preview_token = token_result.get("preview_token")
                    expires_in = token_result.get("expires_in", 600)

                    # 如果只返回了 token，需要手动拼接 URL
                    if not preview_url and preview_token and card_slug:
                        preview_url = f"{_role_card_platform_url(server)}/card/{card_slug}?preview_token={preview_token}"

                    if preview_url:
                        _log.info(f"Preview token generated for card {card_id}, expires in {expires_in}s")

            # 如果没有获取到 preview_url，使用原始URL（不带token）
            if not preview_url:
                preview_url = base_url or f"{_role_card_platform_url(server)}/card/{card_slug}" if card_slug else ""
                if not preview_url:
                    return jsonify({"success": False, "error": "Failed to generate preview URL"}), 500

            return jsonify({
                "success": True,
                "url": preview_url,
                "card": card
            })
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            _log.error(f"Role-card platform rejected upload: {e.code} {detail}")
            return jsonify({"success": False, "error": f"Platform rejected upload: {e.code}"}), 502
        except Exception as e:
            _log.error(f"Upload role card to platform failed: {e}")
            return jsonify({"success": False, "error": f"Upload failed: {str(e)}"}), 502

    @app.route("/api/personality/ai-generate", methods=["POST"])
    def ai_generate_personality():
        """AI 根据用户描述生成角色卡"""
        data = request.json or {}
        description = (data.get("description") or "").strip()

        if not description:
            return jsonify({"success": False, "error": "请提供角色描述"}), 400

        if not server.ai_client:
            return jsonify({"success": False, "error": "AI 客户端未初始化"}), 503

        system_prompt = """你是一个角色卡生成专家。用户会描述一个角色，你需要根据描述生成一个完整的角色卡JSON。

你必须严格按照以下JSON格式返回（不要包含任何额外的文字说明，只返回JSON）：

{
    "name": "角色名称",
    "description": "简短角色描述（用于卡片显示，20字以内）",
    "avatar": "fas fa-star（FontAwesome图标类名，从以下选择最合适的：fas fa-cat, fas fa-dragon, fas fa-hat-wizard, fas fa-skull, fas fa-robot, fas fa-user-secret, fas fa-user-ninja, fas fa-user-astronaut, fas fa-user-graduate, fas fa-user-tie, fas fa-user, fas fa-crown, fas fa-heart, fas fa-star, fas fa-moon, fas fa-sun, fas fa-fire, fas fa-ghost, fas fa-magic, fas fa-shield-haltered, fas fa-wand-sparkles）",
    "tags": ["标签1", "标签2", "标签3"],
    "basicInfo": "角色的基本资料（身高、年龄、职业、外貌、喜好等），每行一项",
    "personality": "性格特点的详细描述",
    "scenario": "角色的背景故事和世界观设定",
    "firstMessage": "新会话中AI自动发送的第一条消息（要符合角色风格）",
    "exampleDialogues": "<user>用户消息示例\\n<assistant>角色回复示例（要符合角色风格和语气）",
    "responseFormat": "期望的回复格式描述，如：（动作描写）对话内容【心情/附加信息】",
    "rules": ["行为规则1", "行为规则2", "行为规则3"],
    "state": {
        "affection": 50,
        "trust": 50,
        "familiarity": 30,
        "dependency": 30,
        "security": 50,
        "mood": "开心"
    }
}

要求：
1. name 必须有创意且贴合描述
2. tags 至少3个，最多5个
3. basicInfo 要具体详细，包含形象特征
4. personality 要生动、有层次
5. scenario 要有沉浸感
6. firstMessage 要符合角色性格，自然不做作
7. exampleDialogues 至少包含2轮对话示例
8. rules 要覆盖角色行为约束和特色
9. state 中的关系初始值必须根据角色设定合理设置：
   - affection（好感）：根据角色对用户的初始态度设置（0-100）
   - trust（信任）：根据角色对用户的初始信任程度设置（0-100）
   - familiarity（熟悉）：根据角色与用户的初始熟悉度设置（0-100）
   - dependency（依赖）：根据角色对用户的初始依赖程度设置（0-100）
   - security（安全感）：根据角色在关系中的初始安全感设置（0-100）
   - mood（心情）：根据角色当前心情设置
10. 所有字段都用中文填写"""

        try:
            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请根据以下描述创建角色卡：\n\n{description}"},
                ],
                stream=False,
            )

            # 记录 token 用量
            try:
                from nbot.core.token_stats import PURPOSE_WEB_FEATURE, get_token_stats_manager
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_WEB_FEATURE,
                    )
            except Exception:
                pass

            content = response.choices[0].message.content.strip()

            # 提取 JSON（可能被 markdown 代码块包裹）
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
            if json_match:
                content = json_match.group(1)

            character = json.loads(content)

            # 填充缺失字段的默认值
            character.setdefault("name", "未命名角色")
            character.setdefault("description", "")
            character.setdefault("avatar", "fas fa-user-circle")
            character.setdefault("tags", [])
            character.setdefault("basicInfo", "")
            character.setdefault("personality", "")
            character.setdefault("scenario", "")
            character.setdefault("firstMessage", "")
            character.setdefault("exampleDialogues", "")
            character.setdefault("responseFormat", "")
            character.setdefault("rules", [])
            # 确保 state 包含所有多维度关系字段
            default_state = {
                "affection": 50,
                "trust": 50,
                "familiarity": 30,
                "dependency": 30,
                "security": 50,
                "mood": "开心"
            }
            ai_state = character.get("state", {})
            if isinstance(ai_state, dict):
                for key, val in default_state.items():
                    ai_state.setdefault(key, val)
                character["state"] = ai_state
            else:
                character["state"] = default_state
            # 立绘图字段 - 由用户后续上传
            character.setdefault("portrait", "")
            character["systemPrompt"] = compile_personality_prompt(character)

            return jsonify({"success": True, "character": character})

        except json.JSONDecodeError as e:
            _log.error(f"AI 生成角色卡 JSON 解析失败: {e}, content: {content[:500] if 'content' in dir() else 'N/A'}")
            return jsonify({"success": False, "error": "AI 生成的角色卡格式有误，请重试"}), 500
        except Exception as e:
            _log.error(f"AI 生成角色卡失败: {e}")
            return jsonify({"success": False, "error": f"生成失败: {str(e)}"}), 500

    @app.route("/api/personality/ai-translate", methods=["POST"])
    def ai_translate_personality():
        """Translate the editable fields of a character card with the configured AI."""
        data = request.json or {}
        character = data.get("character")
        target_language = str(data.get("target_language") or data.get("language") or "en").strip().lower()

        if not isinstance(character, dict):
            return jsonify({"success": False, "error": "角色卡数据无效"}), 400
        if not character.get("name"):
            return jsonify({"success": False, "error": "角色名称不能为空"}), 400
        if not server.ai_client:
            return jsonify({"success": False, "error": "AI 客户端未初始化"}), 503

        language_name = {
            "zh": "Chinese",
            "en": "English",
            "ja": "Japanese",
            "ko": "Korean",
        }.get(target_language, target_language)
        translatable_fields = [
            "name", "description", "basicInfo", "personality", "scenario",
            "firstMessage", "exampleDialogues", "responseFormat", "rules", "tags",
            "systemPrompt",
        ]
        system_prompt = f"""You are a professional character-card translator. Translate the values of the following JSON character card into {language_name}.
Keep every JSON key, data type, array structure, empty field, and line-break structure unchanged. Only translate these fields: {', '.join(translatable_fields)}.
Do not translate avatar/image paths, IDs, or numeric relationship state. Preserve {{{{user}}}}, {{{{char}}}}, <user>, <assistant>, Markdown, variables, and placeholders exactly.
Return only one JSON object, with no Markdown fences or explanation."""
        source = {
            key: value for key, value in character.items()
            if key not in {"id", "avatar", "portrait", "state"}
        }

        try:
            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Translate this character card JSON:\n\n" + json.dumps(source, ensure_ascii=False)},
                ],
                stream=False,
            )

            try:
                from nbot.core.token_stats import PURPOSE_WEB_FEATURE, get_token_stats_manager
                usage = getattr(response, "usage", None)
                if usage:
                    get_token_stats_manager().record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_WEB_FEATURE,
                    )
            except Exception:
                pass

            content = response.choices[0].message.content.strip()
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
            if json_match:
                content = json_match.group(1)
            translated = json.loads(content)
            if not isinstance(translated, dict):
                raise ValueError("AI 返回的翻译结果不是 JSON 对象")

            result = dict(character)
            for field in translatable_fields:
                if field in translated:
                    result[field] = translated[field]
            result["systemPrompt"] = compile_personality_prompt(result)
            return jsonify({"success": True, "character": result})
        except json.JSONDecodeError as e:
            _log.error(f"AI 翻译角色卡 JSON 解析失败: {e}")
            return jsonify({"success": False, "error": "AI 返回的翻译格式有误，请重试"}), 500
        except Exception as e:
            _log.error(f"AI 翻译角色卡失败: {e}")
            return jsonify({"success": False, "error": f"翻译失败: {str(e)}"}), 500

    @app.route("/api/personality/ai-generate-state", methods=["POST"])
    def ai_generate_state():
        """AI 根据当前角色卡内容生成推荐的状态初始值"""
        data = request.json or {}
        character = data.get("character", {})

        if not server.ai_client:
            return jsonify({"success": False, "error": "AI 客户端未初始化"}), 503

        name = character.get("name", "")
        basic_info = character.get("basicInfo", "")
        personality_desc = character.get("personality", "")
        scenario = character.get("scenario", "")

        if not name:
            return jsonify({"success": False, "error": "角色名称不能为空"}), 400

        system_prompt = """你是一个角色关系分析专家。根据提供的角色卡信息，分析该角色对用户的初始关系状态。

你必须严格按照以下JSON格式返回（不要包含任何额外的文字说明，只返回JSON）：

{
    "affection": 50,
    "trust": 50,
    "familiarity": 30,
    "dependency": 30,
    "security": 50,
    "mood": "开心"
}

各字段含义和设置规则：
- affection（好感，0-100）：角色对用户的初始好感度。陌生人通常30-50，友好角色50-70，敌对角色10-30，亲密关系70-90
- trust（信任，0-100）：角色对用户的初始信任度。初次见面通常20-40，坦诚角色40-60，谨慎角色10-30
- familiarity（熟悉，0-100）：角色与用户的初始熟悉度。初次相遇通常10-30，旧识50-80
- dependency（依赖，0-100）：角色对用户的初始依赖程度。独立角色10-30，粘人角色40-60，极度依赖70-90
- security（安全感，0-100）：角色在关系中的初始安全感。自信角色60-80，不安角色20-40
- mood（心情）：角色当前的心情状态，从以下选择：开心、平静、期待、紧张、害羞、委屈、生气、伤心、困倦、得意

分析要求：
1. 必须基于角色的性格、背景和设定来推断初始关系值
2. 不要所有角色都用默认值，要根据角色特点差异化设置
3. 返回纯JSON，不要任何额外文字"""

        user_prompt = f"""请分析以下角色对用户的初始关系状态：

角色名称：{name}

角色基本资料：
{basic_info or '（未提供）'}

角色性格：
{personality_desc or '（未提供）'}

角色背景：
{scenario or '（未提供）'}

请返回推荐的初始关系状态JSON。"""

        try:
            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                stream=False,
            )

            # 记录 token 用量
            try:
                from nbot.core.token_stats import PURPOSE_WEB_FEATURE, get_token_stats_manager
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_WEB_FEATURE,
                    )
            except Exception:
                pass

            content = response.choices[0].message.content.strip()

            # 提取 JSON（可能被 markdown 代码块包裹）
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
            if json_match:
                content = json_match.group(1)

            state = json.loads(content)

            # 验证并填充缺失字段
            default_state = {
                "affection": 50,
                "trust": 50,
                "familiarity": 30,
                "dependency": 30,
                "security": 50,
                "mood": "开心"
            }
            if isinstance(state, dict):
                for key, val in default_state.items():
                    if key not in state:
                        state[key] = val
                    # 数值字段限制在0-100
                    if key != "mood" and isinstance(state[key], (int, float)):
                        state[key] = max(0, min(100, int(state[key])))
                    # mood必须是字符串
                    if key == "mood" and not isinstance(state.get(key), str):
                        state[key] = "开心"
            else:
                state = default_state

            return jsonify({"success": True, "state": state})

        except json.JSONDecodeError as e:
            _log.error(f"AI 生成状态 JSON 解析失败: {e}, content: {content[:500] if 'content' in dir() else 'N/A'}")
            return jsonify({"success": False, "error": "AI 生成的状态格式有误，请重试"}), 500
        except Exception as e:
            _log.error(f"AI 生成状态失败: {e}")
            return jsonify({"success": False, "error": f"生成失败: {str(e)}"}), 500

    @app.route("/api/personality/import", methods=["POST"])
    def import_personality():
        """导入角色卡

        支持：
        - 酒馆 PNG 卡片（tEXt 内嵌 chara/ccv3）
        - 酒馆 chara_card_v1/v2/v3 JSON
        - 本项目原生角色卡 JSON
        - ZIP 包（character.json + portrait.*）
        """
        if "file" not in request.files:
            return jsonify({"success": False, "error": "请上传文件"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "error": "文件名为空"}), 400

        try:
            blob = file.read()
            filename = (file.filename or "").lower()
            portrait_bytes = None
            portrait_ext = ".png"
            meta = {"format": "native", "warnings": []}

            if filename.endswith(".zip"):
                character, meta, portrait_bytes, portrait_ext = _parse_card_zip(blob)

            elif tavern_card.is_png(blob) or filename.endswith(".png"):
                character, meta = tavern_card.parse_card_bytes(file.filename, blob)
                # 酒馆 PNG 卡片的图片本身就是立绘
                portrait_bytes = blob

            elif filename.endswith(".json"):
                character, meta = tavern_card.parse_card_bytes(file.filename, blob)

            else:
                return jsonify({
                    "success": False,
                    "error": "不支持的文件格式，请上传 .png、.json 或 .zip 文件",
                }), 400

            if portrait_bytes:
                try:
                    character["portrait"] = _save_portrait_bytes(server, portrait_bytes, portrait_ext)
                except Exception as e:
                    _log.error(f"立绘导入失败: {e}")

            character = _finalize_imported_card(character, meta)

            return jsonify({
                "success": True,
                "character": character,
                "meta": {
                    "format": meta.get("format", "native"),
                    "warnings": meta.get("warnings", []),
                    "alternate_greetings": character.get("alternateGreetings", []),
                    "character_book": meta.get("character_book"),
                },
            })

        except tavern_card.TavernCardError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except json.JSONDecodeError:
            return jsonify({"success": False, "error": "文件不是有效的 JSON 格式"}), 400
        except UnicodeDecodeError:
            return jsonify({"success": False, "error": "文件编码不支持，请使用 UTF-8"}), 400
        except zipfile.BadZipFile:
            return jsonify({"success": False, "error": "ZIP 文件损坏或格式不正确"}), 400
        except Exception as e:
            _log.error(f"导入角色卡失败: {e}")
            return jsonify({"success": False, "error": f"导入失败: {str(e)}"}), 500

    @app.route("/api/personality/import-character-book", methods=["POST"])
    def import_character_book():
        """把角色卡内嵌的世界书（character_book）落地为独立世界书。"""
        from nbot.character.storage.world_book_store import WorldBookStore

        data = request.json or {}
        book_data = data.get("book") or data.get("character_book") or {}
        if not isinstance(book_data, dict):
            return jsonify({"success": False, "error": "世界书数据格式有误"}), 400

        entries = book_data.get("entries")
        if not isinstance(entries, list) or not entries:
            return jsonify({"success": False, "error": "世界书没有可导入的条目"}), 400

        character_name = str(data.get("character_name") or "").strip()
        character_id = str(data.get("character_id") or "").strip()
        book_name = str(book_data.get("name") or "").strip()
        if not book_name:
            book_name = f"{character_name or '角色'} 设定集"

        try:
            store = WorldBookStore(server.base_dir)
            book = store.create(
                name=book_name,
                description=str(book_data.get("description") or ""),
                character_ids=[character_id] if character_id else [],
            )
            created = store.batch_add_entries(book.id, entries)
            _log.info(
                "[ImportBook] created world book %s (%s) with %d entries",
                book.id,
                book_name,
                len(created),
            )
            return jsonify({
                "success": True,
                "world_book": {
                    "id": book.id,
                    "name": book.name,
                    "entry_count": len(created),
                },
                "entry_count": len(created),
            })
        except Exception as e:
            _log.error(f"导入世界书失败: {e}")
            return jsonify({"success": False, "error": f"导入世界书失败: {str(e)}"}), 500

    @app.route("/api/personality/greeting", methods=["POST"])
    def pick_personality_greeting():
        """从角色卡的开场白库中挑选一条（主开场白 + 备用开场白）。"""
        data = request.json or {}
        character = data.get("character") if isinstance(data.get("character"), dict) else data
        if not isinstance(character, dict):
            return jsonify({"success": False, "error": "角色卡数据格式有误"}), 400

        index = data.get("index")
        randomize = bool(data.get("random"))
        text, picked = tavern_card.pick_greeting(character, index=index, randomize=randomize)

        user_name = str(data.get("user") or "").strip()
        char_name = str(character.get("name") or "").strip()
        if user_name:
            text = text.replace("{{user}}", user_name)
        if char_name:
            text = text.replace("{{char}}", char_name)

        return jsonify({
            "success": True,
            "index": picked,
            "text": text,
            "count": len(tavern_card.extract_greetings(character)),
        })

    @app.route("/api/personality/ai-generate-first-message", methods=["POST"])
    def ai_generate_first_message():
        """AI 根据角色设定随机生成开场白"""
        data = request.json or {}

        if not server.ai_client:
            return jsonify({"success": False, "error": "AI 客户端未初始化"}), 503

        name = data.get("name", "")
        basic_info = data.get("basicInfo", "")
        personality = data.get("personality", "")
        scenario = data.get("scenario", "")
        target_id = data.get("target_id") or data.get("session_id") or ""
        memory_context, memory_count = _format_first_message_memories(server, name, target_id)

        if not name:
            return jsonify({"success": False, "error": "请先填写角色名称"}), 400

        char_context = f"角色名称：{name}"
        if basic_info:
            char_context += f"\n基本信息：{basic_info}"
        if personality:
            char_context += f"\n性格特点：{personality}"
        if scenario:
            char_context += f"\n背景设定：{scenario}"

        if memory_context:
            char_context += memory_context
        _log.info(
            "[FirstMessage] generating with %d memories: character=%s target=%s",
            memory_count,
            name,
            target_id or "-",
        )

        try:
            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个角色扮演游戏的开场白设计师。根据角色设定，生成一个生动、自然、有画面感的开场白。\n\n"
                            "要求：\n"
                            "- 用括号描写角色的动作、神态（如：（微微一笑）（推了推眼镜））\n"
                            "- 语气要符合角色性格\n"
                            "- 自然口语化，不要朗诵腔\n"
                            "- 30-80字\n"
                            "- 不同风格各不同，不要每次都生成类似的\n"
                            "- 直接返回开场白，不要引号或解释"
                            "-------------------------------------\n"
                            "如果下面的角色上下文中包含“当前已有的对应角色记忆”，"
                            "你必须根据这些记忆来写开场白：至少自然融入1-2个具体记忆事实、关系状态或共同经历。并且以最新的记忆优先"
                            "不要写成泛泛的初次见面寒暄；不要说明你读取了记忆或提到系统注入。"
                            "如果没有记忆，再只依据角色设定和场景生成。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"请为以下角色随机生成一个开场白：\n\n{char_context}",
                    },
                ],
                stream=False,
            )

            # 记录 token 用量
            try:
                from nbot.core.token_stats import PURPOSE_WEB_FEATURE, get_token_stats_manager
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_WEB_FEATURE,
                    )
            except Exception:
                pass

            result = response.choices[0].message.content.strip()
            result = result.strip("\"'「」『』【】()（）")

            return jsonify({"success": True, "firstMessage": result})

        except Exception as e:
            _log.error(f"AI 生成开场白失败: {e}")
            return jsonify({"success": False, "error": f"生成失败: {str(e)}"}), 500

    @app.route("/api/personality/portrait", methods=["POST"])
    def upload_personality_portrait():
        """上传角色立绘图片，返回图片URL"""
        if "file" not in request.files:
            return jsonify({"success": False, "error": "请上传文件"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "error": "文件名为空"}), 400

        if not allowed_image_file(file.filename):
            return jsonify({"success": False, "error": "不支持的文件格式，请上传图片文件"}), 400

        try:
            # 创建上传目录
            upload_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
            os.makedirs(upload_dir, exist_ok=True)

            # 生成唯一文件名
            ext = file.filename.rsplit(".", 1)[1].lower()
            filename = f"portrait_{uuid.uuid4().hex[:16]}.{ext}"
            filepath = os.path.join(upload_dir, filename)

            # 保存文件
            file.save(filepath)

            # 返回图片URL
            image_url = f"/static/uploads/portraits/{filename}"
            _log.info(f"立绘上传成功: {filepath}")

            return jsonify({"success": True, "url": image_url})

        except Exception as e:
            _log.error(f"上传立绘失败: {e}")
            return jsonify({"success": False, "error": f"上传失败: {str(e)}"}), 500

    @app.route("/api/personality/portrait", methods=["DELETE"])
    def delete_personality_portrait():
        """删除角色立绘图片"""
        data = request.json or {}
        portrait_url = data.get("url", "")

        if not portrait_url:
            return jsonify({"success": False, "error": "未提供图片URL"}), 400

        try:
            # 从URL中提取文件名
            if portrait_url.startswith("/static/uploads/portraits/"):
                filename = os.path.basename(portrait_url)
                filepath = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits", filename)

                if os.path.exists(filepath):
                    os.remove(filepath)
                    _log.info(f"立绘删除成功: {filepath}")

            return jsonify({"success": True})

        except Exception as e:
            _log.error(f"删除立绘失败: {e}")
            return jsonify({"success": False, "error": f"删除失败: {str(e)}"}), 500

    @app.route("/api/personality/export", methods=["POST"])
    def export_personality():
        """导出角色卡

        format:
        - native（默认）：ZIP，包含本项目 character.json 与立绘
        - tavern_v2 / tavern_v3：酒馆兼容 JSON（chara_card_v2/v3）
        - tavern_png：内嵌卡片数据的 PNG（无立绘时使用占位图）
        """
        data = request.json or {}
        character = data.get("character", {})

        if not character.get("name"):
            return jsonify({"success": False, "error": "角色卡缺少名称"}), 400

        fmt = str(data.get("format") or "native").strip().lower()
        character_name = character.get('name', 'character')
        safe_name = re.sub(r'[^\w\s-]', '', character_name).strip() or "character"

        try:
            character_book = data.get("character_book")
            if not isinstance(character_book, dict):
                character_book = character.get("characterBook")
            world_book_id = str(data.get("world_book_id") or "").strip()
            if world_book_id:
                character_book = _load_world_book_payload(server, world_book_id) or character_book

            if fmt in {"tavern_v2", "tavern_v3", "tavern", "v2", "v3"}:
                version = "v3" if fmt in {"tavern_v3", "v3"} else "v2"
                card = tavern_card.to_tavern_card(
                    character, version, character_book=character_book
                )
                payload = json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8")
                return send_file(
                    io.BytesIO(payload),
                    mimetype="application/json",
                    as_attachment=True,
                    download_name=f"{safe_name}_{version}.json",
                )

            if fmt in {"tavern_png", "png", "card_png"}:
                base_image = _read_local_portrait_png(server, character.get("portrait", ""))
                if not base_image:
                    _log.info("[Export] 无 PNG 立绘，使用占位图导出 %s", character_name)
                png = tavern_card.build_card_png(
                    {**character,
                     "characterBook": character_book if isinstance(character_book, dict) else None},
                    base_image or None,
                )
                return send_file(
                    io.BytesIO(png),
                    mimetype="image/png",
                    as_attachment=True,
                    download_name=f"{safe_name}_角色卡.png",
                )

            # 创建内存中的 ZIP 文件
            memory_file = io.BytesIO()

            with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
                # 1. 添加 JSON 文件（不包含立绘原始数据，只保留 URL）
                character_json = json.dumps(character, ensure_ascii=False, indent=2)
                zf.writestr('character.json', character_json)

                # 2. 如果有立绘图片，添加到 ZIP
                portrait_url = character.get('portrait', '')
                if portrait_url and portrait_url.startswith('/static/'):
                    # 从 URL 获取本地文件路径
                    portrait_path = portrait_url.replace('/static/', '')
                    full_path = os.path.join(server.base_dir, 'nbot', 'web', 'static', portrait_path.replace('static/', ''))

                    if os.path.exists(full_path):
                        # 获取文件扩展名
                        _, ext = os.path.splitext(full_path)
                        # 添加到 ZIP，使用固定名称
                        zf.write(full_path, f'portrait{ext}')

            # 准备响应
            memory_file.seek(0)

            return send_file(
                memory_file,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'{safe_name}_角色卡.zip'
            )

        except tavern_card.TavernCardError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            _log.error(f"导出角色卡失败: {e}")
            return jsonify({"success": False, "error": f"导出失败: {str(e)}"}), 500

    @app.route("/api/personality/export-all", methods=["GET"])
    def export_all_personalities():
        """导出所有自定义角色卡为 ZIP 文件

        query:
        - format=native（默认）｜tavern_v2｜tavern_v3
        """
        try:
            export_format = str(request.args.get("format") or "native").strip().lower()
            tavern_version = ""
            if export_format in {"tavern_v2", "tavern_v3"}:
                tavern_version = "v3" if export_format == "tavern_v3" else "v2"
            # 从文件加载自定义角色预设
            presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
            if os.path.exists(presets_file):
                try:
                    with open(presets_file, encoding="utf-8") as f:
                        presets = json.load(f)
                except Exception as e:
                    _log.error(f"加载自定义角色卡预设文件失败: {e}")
                    presets = []
            else:
                presets = []

            if not presets:
                return jsonify({"success": False, "error": "没有可导出的角色卡"}), 400

            # 创建内存中的 ZIP 文件
            memory_file = io.BytesIO()

            with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
                for preset in presets:
                    # 构建角色卡数据
                    character = {
                        "name": preset.get("name", ""),
                        "description": preset.get("description", ""),
                        "avatar": preset.get("avatar", "fas fa-user-circle"),
                        "tags": preset.get("tags", []),
                        "systemPrompt": preset.get("systemPrompt", ""),
                        "basicInfo": preset.get("basicInfo", ""),
                        "personality": preset.get("personality", ""),
                        "scenario": preset.get("scenario", ""),
                        "firstMessage": preset.get("firstMessage", ""),
                        "exampleDialogues": preset.get("exampleDialogues", ""),
                        "responseFormat": preset.get("responseFormat", ""),
                        "rules": preset.get("rules", []),
                        "state": preset.get("state", {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}),
                        "alternateGreetings": preset.get("alternateGreetings", []),
                    }
                    if isinstance(preset.get("tavern"), dict):
                        character["tavern"] = preset["tavern"]

                    # 创建角色专属文件夹名称（安全化文件名）
                    preset_id = preset.get("id", str(uuid.uuid4()))
                    safe_name = re.sub(r'[^\w\s-]', '', preset.get("name", "character")).strip() or f"character_{preset_id}"
                    folder_name = f"{safe_name}_{preset_id}"

                    # 添加 JSON 文件
                    if tavern_version:
                        book_payload = _load_world_book_payload(
                            server, str(preset.get("characterBookId") or "")
                        )
                        payload = tavern_card.to_tavern_card(
                            character,
                            tavern_version,
                            character_book=book_payload or None,
                        )
                        character_json = json.dumps(payload, ensure_ascii=False, indent=2)
                    else:
                        character_json = json.dumps(character, ensure_ascii=False, indent=2)
                    zf.writestr(f'{folder_name}/character.json', character_json)

                    # 如果有立绘图片，添加到 ZIP
                    portrait = preset.get("portrait", "")
                    if portrait and portrait.startswith('/static/'):
                        portrait_path = portrait.replace('/static/', '')
                        full_path = os.path.join(server.base_dir, 'nbot', 'web', 'static', portrait_path.replace('static/', ''))

                        if os.path.exists(full_path):
                            _, ext = os.path.splitext(full_path)
                            zf.write(full_path, f'{folder_name}/portrait{ext}')

            # 准备响应
            memory_file.seek(0)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

            return send_file(
                memory_file,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'全部角色卡_{timestamp}.zip'
            )

        except Exception as e:
            _log.error(f"导出全部角色卡失败: {e}")
            return jsonify({"success": False, "error": f"导出失败: {str(e)}"}), 500

    @app.route("/api/personality/import-all", methods=["POST"])
    def import_all_personalities():
        """批量导入角色卡，支持包含多个角色卡文件夹的 ZIP 文件"""
        if "file" not in request.files:
            return jsonify({"success": False, "error": "请上传文件"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "error": "文件名为空"}), 400

        try:
            filename = file.filename.lower()
            if not filename.endswith('.zip'):
                return jsonify({"success": False, "error": "请上传 ZIP 格式的文件"}), 400

            # 读取 ZIP 文件
            zip_data = io.BytesIO(file.read())
            imported_count = 0
            failed_count = 0
            failed_names = []

            # 加载现有角色卡
            presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
            if os.path.exists(presets_file):
                try:
                    with open(presets_file, encoding="utf-8") as f:
                        existing_presets = json.load(f)
                except Exception as e:
                    _log.error(f"加载自定义角色卡预设文件失败: {e}")
                    existing_presets = []
            else:
                existing_presets = []

            with zipfile.ZipFile(zip_data, 'r') as zf:
                # 查找所有 character.json 文件
                character_folders = {}
                for name in zf.namelist():
                    if name.endswith('character.json'):
                        # 获取文件夹名称
                        folder = name.rsplit('/', 1)[0] if '/' in name else ''
                        character_folders[folder] = {'json': name, 'portrait': None}

                # 查找对应的立绘文件
                for name in zf.namelist():
                    if 'portrait.' in name.lower():
                        folder = name.rsplit('/', 1)[0] if '/' in name else ''
                        if folder in character_folders:
                            character_folders[folder]['portrait'] = name

                for folder, files in character_folders.items():
                    try:
                        # 读取 JSON（原生卡或酒馆 v1/v2/v3 卡统一归一化）
                        raw_character = json.loads(zf.read(files['json']).decode('utf-8'))
                        character, _card_meta = tavern_card.normalize_tavern_card(raw_character)

                        # 验证基本字段
                        if not character.get("name"):
                            failed_count += 1
                            failed_names.append(f"{folder}: 缺少角色名称")
                            continue

                        # 处理立绘图片
                        portrait_url = ''
                        if files['portrait']:
                            try:
                                portrait_data = zf.read(files['portrait'])
                                portrait_ext = os.path.splitext(files['portrait'])[1]

                                # 创建上传目录
                                upload_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
                                os.makedirs(upload_dir, exist_ok=True)

                                # 生成唯一文件名
                                new_filename = f"portrait_{uuid.uuid4().hex[:16]}{portrait_ext}"
                                filepath = os.path.join(upload_dir, new_filename)

                                # 保存文件
                                with open(filepath, 'wb') as f:
                                    f.write(portrait_data)

                                portrait_url = f"/static/uploads/portraits/{new_filename}"
                                _log.info(f"立绘导入成功: {filepath}")
                            except Exception as e:
                                _log.error(f"立绘导入失败: {e}")

                        # 检查是否已存在同名角色
                        existing_preset = None
                        for i, p in enumerate(existing_presets):
                            if p.get("name") == character.get("name"):
                                existing_preset = i
                                break

                        # 构建角色卡数据
                        new_preset_data = {
                            "id": str(uuid.uuid4()),
                            "name": character.get("name"),
                            "description": character.get("description", ""),
                            "avatar": character.get("avatar", "fas fa-user-circle"),
                            "tags": character.get("tags", []),
                            "basicInfo": character.get("basicInfo", ""),
                            "personality": character.get("personality", ""),
                            "scenario": character.get("scenario", ""),
                            "firstMessage": character.get("firstMessage", ""),
                            "exampleDialogues": character.get("exampleDialogues", ""),
                            "responseFormat": character.get("responseFormat", ""),
                            "rules": character.get("rules", []),
                            "state": character.get("state", {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"}),
                            "alternateGreetings": character.get("alternateGreetings", []),
                            "created_at": datetime.now().isoformat(),
                        }
                        if isinstance(character.get("tavern"), dict) and character["tavern"]:
                            new_preset_data["tavern"] = character["tavern"]
                        if portrait_url:
                            new_preset_data["portrait"] = portrait_url
                        new_preset_data["systemPrompt"] = compile_personality_prompt(new_preset_data)

                        if existing_preset is not None:
                            # 更新现有角色（保留id和created_at）
                            new_preset_data["id"] = existing_presets[existing_preset].get("id", new_preset_data["id"])
                            new_preset_data["created_at"] = existing_presets[existing_preset].get("created_at", new_preset_data["created_at"])
                            existing_presets[existing_preset] = new_preset_data
                        else:
                            # 创建新角色
                            existing_presets.append(new_preset_data)

                        imported_count += 1

                    except Exception as e:
                        failed_count += 1
                        failed_names.append(f"{folder}: {str(e)}")
                        _log.error(f"导入角色卡失败 {folder}: {e}")

            # 保存到文件
            server.custom_personality_presets = existing_presets
            server._save_data("custom_personality_presets")

            result_message = f"成功导入 {imported_count} 个角色卡"
            if failed_count > 0:
                result_message += f"，失败 {failed_count} 个"

            return jsonify({
                "success": True,
                "message": result_message,
                "imported_count": imported_count,
                "failed_count": failed_count,
                "failed_names": failed_names
            })

        except zipfile.BadZipFile:
            return jsonify({"success": False, "error": "ZIP 文件损坏或格式不正确"}), 400
        except Exception as e:
            _log.error(f"批量导入角色卡失败: {e}")
            return jsonify({"success": False, "error": f"导入失败: {str(e)}"}), 500

    @app.route("/api/personality/generate-portrait", methods=["POST"])
    def generate_portrait():
        """使用AI生成角色立绘"""
        data = request.json or {}
        character_name = data.get("character_name", "").strip()
        description = data.get("description", "").strip()
        basic_info = data.get("basic_info", "").strip()
        personality = data.get("personality", "").strip()

        if not character_name:
            return jsonify({"success": False, "error": "请提供角色名称"}), 400

        # 检查是否配置了图片生成模型
        image_gen_config = None
        if hasattr(server, 'active_models_by_purpose'):
            image_gen_model_id = server.active_models_by_purpose.get('image_generation')
            if image_gen_model_id and hasattr(server, 'ai_models'):
                for model in server.ai_models:
                    if model.get('id') == image_gen_model_id:
                        image_gen_config = model
                        break

        if not image_gen_config:
            return jsonify({
                "success": False,
                "need_config": True,
                "error": "未配置图片生成模型，请先在AI配置中添加图片生成模型"
            }), 400

        # 创建任务并立即返回，实际生成在后台线程执行
        task_id = f"portrait_{uuid.uuid4().hex[:16]}"
        server.portrait_generation_tasks[task_id] = {
            "status": "processing",
            "character_name": character_name,
            "portrait_url": None,
            "error": None,
            "updated_at": datetime.now().isoformat()
        }

        # 启动后台生成任务
        server.socketio.start_background_task(
            _do_generate_portrait_bg,
            server, task_id, character_name, description,
            basic_info, personality, image_gen_config
        )

        return jsonify({
            "success": True,
            "task_id": task_id,
            "status": "processing",
            "message": "立绘生成已提交，正在后台处理"
        })

    @app.route("/api/personality/generate-portrait/<task_id>", methods=["GET"])
    def get_portrait_generation_status(task_id):
        """查询立绘生成任务状态"""
        task = server.portrait_generation_tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在或已过期"}), 404
        return jsonify({"success": True, **task})

    @app.route("/api/personality/pending-portraits", methods=["GET"])
    def get_pending_portraits():
        """查询指定角色是否有后台生成的待处理立绘"""
        character_name = request.args.get("character_name", "").strip()
        if not character_name:
            return jsonify({"success": False, "error": "请提供角色名称"}), 400

        portrait_url = server.pending_portraits.get(character_name)
        if portrait_url:
            return jsonify({
                "success": True,
                "portrait_url": portrait_url,
                "has_pending": True
            })
        return jsonify({
            "success": True,
            "portrait_url": None,
            "has_pending": False
        })

    @app.route("/api/personality/clean-unused-portraits", methods=["POST"])
    def clean_unused_portraits():
        """清理未被任何角色卡使用的立绘文件"""
        try:
            portraits_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
            if not os.path.exists(portraits_dir):
                return jsonify({"success": True, "deleted_count": 0, "message": "没有立绘文件需要清理"})

            # 收集所有正在使用的立绘URL
            used_portraits = set()

            # 1. 当前角色的立绘
            current_portrait = server.personality.get("portrait", "")
            if current_portrait:
                used_portraits.add(current_portrait)

            # 2. 自定义角色预设中的立绘
            presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
            if os.path.exists(presets_file):
                try:
                    with open(presets_file, encoding="utf-8") as f:
                        presets = json.load(f)
                    for preset in presets:
                        portrait = preset.get("portrait", "")
                        if portrait:
                            used_portraits.add(portrait)
                except Exception as e:
                    _log.error(f"加载角色预设文件失败: {e}")

            # 3. 检查会话中的立绘引用（会话可能引用角色立绘）
            if hasattr(server, 'sessions'):
                for session in server.sessions.values():
                    portrait = session.get("sender_portrait", "")
                    if portrait:
                        used_portraits.add(portrait)

            # 扫描立绘目录，找出未使用的文件
            all_portraits = os.listdir(portraits_dir)
            unused_portraits = []
            deleted_count = 0

            for filename in all_portraits:
                if not allowed_image_file(filename):
                    continue

                portrait_url = f"/static/uploads/portraits/{filename}"
                if portrait_url not in used_portraits:
                    filepath = os.path.join(portraits_dir, filename)
                    try:
                        file_size = os.path.getsize(filepath)
                        os.remove(filepath)
                        deleted_count += 1
                        unused_portraits.append({
                            "filename": filename,
                            "size": file_size
                        })
                        _log.info(f"清理未使用立绘: {filepath}")
                    except Exception as e:
                        _log.error(f"删除立绘失败 {filepath}: {e}")

            total_saved = sum(p["size"] for p in unused_portraits)
            message = f"已清理 {deleted_count} 个未使用的立绘文件，释放 {total_saved / 1024:.1f} KB 空间"

            return jsonify({
                "success": True,
                "deleted_count": deleted_count,
                "total_saved": total_saved,
                "message": message
            })

        except Exception as e:
            _log.error(f"清理未使用立绘失败: {e}")
            return jsonify({"success": False, "error": f"清理失败: {str(e)}"}), 500
