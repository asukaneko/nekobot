import logging
import os
import uuid
from datetime import datetime

from flask import jsonify, request

_log = logging.getLogger(__name__)


def compile_personality_prompt(personality_data, session_context=None):
    """将角色卡JSON编译成系统提示词"""
    prompt = ''

    name = personality_data.get('name', '')
    basic_info = personality_data.get('basicInfo', '')
    personality = personality_data.get('personality', '')
    scenario = personality_data.get('scenario', '')
    response_format = personality_data.get('responseFormat', '')
    rules = personality_data.get('rules', [])
    example_dialogues = personality_data.get('exampleDialogues', '')

    # 角色设定部分
    if name:
        prompt += f'【角色名称】{name}\n'
    if basic_info:
        prompt += f'【基本信息】\n{basic_info}\n'
    if personality:
        prompt += f'【性格特点】{personality}\n'
    if scenario:
        prompt += f'【背景设定】{scenario}\n'
    if response_format:
        prompt += f'【回复格式】{response_format}\n'
    if rules and len(rules) > 0:
        prompt += '【行为规则】\n'
        for i, rule in enumerate(rules, 1):
            if rule:
                prompt += f'{i}. {rule}\n'
    if example_dialogues:
        prompt += f'【示例对话】\n{example_dialogues}\n'
    
    # 会话上下文
    if session_context:
        prompt += '\n【当前会话上下文】\n'
        if 'session_name' in session_context:
            prompt += f"会话名称: {session_context['session_name']}\n"
        if 'current_time' in session_context:
            prompt += f"当前时间: {session_context['current_time']}\n"
        if 'user_info' in session_context:
            prompt += f"用户信息: {session_context['user_info']}\n"
        if 'recent_messages' in session_context:
            prompt += "近期对话:\n"
            for msg in session_context['recent_messages']:
                prompt += f"  {msg}\n"
    
    # 角色状态
    state = personality_data.get('state', {})
    if state:
        prompt += '\n【角色当前状态】\n'
        if 'affection' in state:
            prompt += f"好感度: {state['affection']}/100\n"
        if 'mood' in state:
            prompt += f"心情: {state['mood']}\n"
    
    if prompt:
        prompt = f'你是角色 "{name or "未命名"}"。\n\n' + prompt
    else:
        prompt = '请定义你的角色设定。'
    
    return prompt


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
        
        server.personality["systemPrompt"] = data.get("systemPrompt", server.personality.get("systemPrompt", ""))
        server.personality["basicInfo"] = data.get("basicInfo", server.personality.get("basicInfo", ""))
        server.personality["personality"] = data.get("personality", server.personality.get("personality", ""))
        server.personality["scenario"] = data.get("scenario", server.personality.get("scenario", ""))
        server.personality["firstMessage"] = data.get("firstMessage", server.personality.get("firstMessage", ""))
        server.personality["exampleDialogues"] = data.get("exampleDialogues", server.personality.get("exampleDialogues", ""))
        
        server.personality["responseFormat"] = data.get("responseFormat", server.personality.get("responseFormat", ""))
        server.personality["rules"] = data.get("rules", server.personality.get("rules", []))
        
        server.personality["state"] = data.get("state", server.personality.get("state", {"affection": 50, "mood": "开心"}))
        server.personality["greeting"] = data.get("greeting", server.personality.get("greeting", ""))

        if not server.personality.get("systemPrompt"):
            server.personality["systemPrompt"] = compile_personality_prompt(server.personality)

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
                "state": {"affection": 50, "mood": "开心"}
            },
        ]
        return jsonify(presets)

    @app.route("/api/personality/custom-presets", methods=["GET"])
    def get_custom_personality_presets():
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
            "state": data.get("state", {"affection": 50, "mood": "开心"}),
            "created_at": datetime.now().isoformat(),
        }
        preset["systemPrompt"] = compile_personality_prompt(preset)
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
