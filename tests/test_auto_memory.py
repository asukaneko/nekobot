from types import SimpleNamespace

from nbot.core.auto_memory import parse_memory_response


def test_parse_memory_response_empty_array():
    assert parse_memory_response("[]") == []


def test_parse_memory_response_code_fence():
    text = """```json
[
  {
    "title": "喜欢的称呼",
    "summary": "用户喜欢被叫主人",
    "content": "用户喜欢角色称呼自己为主人。",
    "type": "long"
  }
]
```"""
    result = parse_memory_response(text)

    assert len(result) == 1
    assert result[0]["title"] == "喜欢的称呼"
    assert result[0]["type"] == "long"


def test_parse_memory_response_filters_incomplete_items():
    text = '[{"summary": "缺少内容"}, {"title": "偏好", "content": "用户喜欢轻松语气"}]'
    result = parse_memory_response(text)

    assert len(result) == 2
    assert result[0]["title"] == "缺少内容"
    assert result[1]["summary"] == "用户喜欢轻松语气"


def test_parse_memory_response_keeps_structured_categories():
    text = """{
      "memories": [
        {
          "category": "user_persona",
          "title": "互动边界",
          "summary": "用户偏好克制安慰",
          "content": "用户在情绪低落时更希望角色先安静陪伴。",
          "type": "long",
          "importance": 0.8
        },
        {
          "category": "character_persona",
          "title": "角色态度",
          "summary": "角色对用户更谨慎",
          "content": "角色意识到用户不喜欢被过度追问。",
          "type": "long",
          "importance": 0.7
        },
        {
          "category": "important_event",
          "title": "雨夜约定",
          "summary": "两人约定下次见面",
          "content": "用户和角色约定下次在雨夜继续谈话。",
          "type": "long",
          "importance": 0.9
        },
        {
          "category": "recent_digest",
          "title": "近期摘要",
          "summary": "本段对话围绕安慰方式和下次约定。",
          "content": "用户表达了对陪伴方式的偏好，并与角色形成下次见面的约定。",
          "type": "short",
          "importance": 0.4
        }
      ]
    }"""

    result = parse_memory_response(text)

    assert [item["category"] for item in result] == [
        "user_persona",
        "character_persona",
        "important_event",
        "recent_digest",
    ]
    assert result[0]["importance"] == 0.8


def test_extract_and_save_turn_memories_syncs_structured_items_to_memory_fs(
    monkeypatch,
    tmp_path,
):
    import nbot.core.auto_memory as auto_memory
    import nbot.memory.fs as fs_mod
    from nbot.memory.fs import MemoryFS

    mfs = MemoryFS(data_dir=str(tmp_path))
    monkeypatch.setattr(fs_mod, "_memory_fs", mfs)
    monkeypatch.setattr(auto_memory, "_MEMORY_TURN_INTERVAL", 1)
    auto_memory._MEMORY_TURN_COUNTERS.clear()
    auto_memory._MEMORY_TURN_BUFFER.clear()

    calls = []

    def fake_call_memory_model(turns, character_name="", user_name="", language="", existing_sections=None):
        calls.append(turns)
        return [
            {
                "category": "user_persona",
                "title": "互动边界",
                "summary": "用户偏好克制安慰",
                "content": "用户在情绪低落时更希望角色先安静陪伴。",
                "type": "long",
                "importance": 0.8,
            },
            {
                "category": "character_persona",
                "title": "角色态度",
                "summary": "角色对用户更谨慎",
                "content": "角色意识到用户不喜欢被过度追问。",
                "type": "long",
                "importance": 0.7,
            },
            {
                "category": "important_event",
                "title": "雨夜约定",
                "summary": "两人约定下次见面",
                "content": "用户和角色约定下次在雨夜继续谈话。",
                "type": "long",
                "importance": 0.9,
            },
            {
                "category": "recent_digest",
                "title": "近期摘要",
                "summary": "安慰方式和下次约定",
                "content": "用户表达了对陪伴方式的偏好，并与角色形成下次见面的约定。",
                "type": "short",
                "importance": 0.4,
            },
        ]

    monkeypatch.setattr(auto_memory, "_call_memory_model", fake_call_memory_model)

    ctx = SimpleNamespace(
        chat_request=SimpleNamespace(
            content="我难过的时候别一直问我原因，陪我待一会儿就好。",
            user_id="user-1",
            metadata={"character_id": "char-1", "character_name": "角色一"},
        ),
        metadata={},
    )
    callbacks = SimpleNamespace(
        get_memory_context=lambda _ctx: {
            "character_id": "char-1",
            "character_name": "角色一",
            "target_id": "user-1",
            "session_id": "conv-1",
        }
    )
    result = SimpleNamespace(final_content="我会记住，先陪你安静待一会儿。", error=None)

    saved_count = auto_memory.extract_and_save_turn_memories(ctx, callbacks, result)

    assert saved_count == 4
    assert len(calls) == 1
    assert "克制安慰" in mfs.read(mfs.path_user_persona("char-1", "user-1")).summary
    assert "更谨慎" in mfs.read(mfs.path_character_persona("char-1", "user-1")).summary
    assert "雨夜约定" in mfs.read(mfs.path_important_events("char-1", "conv-1")).title
    digest = mfs.read(mfs.path_recent_digest("char-1", "user-1"))
    assert digest is not None
    assert "下次见面" in digest.content
