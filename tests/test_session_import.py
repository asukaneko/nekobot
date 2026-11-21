"""Tests for DeepSeek official session import (nbot/web/session_import/).

All fixtures are synthetic placeholders: no real conversations, accounts or file paths.
"""

import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest
from flask import Flask

from nbot.web.routes.session_import import register_session_import_routes
from nbot.web.session_import import (
    ArchiveStaging,
    DeepSeekArchiveReader,
    SessionImportError,
    list_session_import_formats,
)
from nbot.web.session_import import deepseek as deepseek_module
from nbot.web.session_import.payload import extract_export_sessions, normalize_imported_session
from nbot.web.session_import.url_policy import require_public_https_url

# ----------------------------------------------------------------------------- helpers


def _archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            archive.writestr(name, payload)
    return buffer.getvalue()


def _session_json(conversation_id, title, message_count=2, pinned=False):
    messages = []
    for index in range(message_count):
        is_user = index % 2 == 0
        messages.append(
            {
                "id": index + 1,
                "role": "USER" if is_user else "ASSISTANT",
                "inserted_at": f"2026-01-02T03:04:{index:02d}.000Z",
                "fragments": [
                    {
                        "type": "REQUEST" if is_user else "RESPONSE",
                        "content": f"合成消息 {index}",
                    }
                ],
            }
        )
    return {
        "id": conversation_id,
        "title": title,
        "pinned": pinned,
        "inserted_at": "2026-01-02T03:04:00.000Z",
        "updated_at": "2026-01-02T03:05:00.000Z",
        "messages": messages,
    }


def _index(rows):
    return json.dumps(rows, ensure_ascii=False)


def _local(stamp):
    """把导出里的 UTC 时间戳换算成本地 naive 时间（与解析器一致）。"""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment.astimezone().replace(tzinfo=None)


def _stamp(value):
    return datetime.fromisoformat(value)


def fragments_session():
    return {
        "id": "session-fragments",
        "title": "片段示例",
        "pinned": False,
        "inserted_at": "2026-01-02T03:04:05.000Z",
        "updated_at": "2026-01-02T03:04:07.000Z",
        "messages": [
            {
                "id": 1,
                "role": "USER",
                "inserted_at": "2026-01-02T03:04:06.000Z",
                "fragments": [
                    {"type": "REQUEST", "content": "合成的用户提问"},
                    {"type": "FILE", "files": [{"file_name": "合成图片.png"}]},
                ],
            },
            {
                "id": 2,
                "role": "ASSISTANT",
                "inserted_at": "2026-01-02T03:04:07.000Z",
                "fragments": [
                    {"type": "THINK", "content": "合成的推理过程"},
                    {"type": "RESPONSE", "content": "合成的助手回答"},
                    {"type": "CONTENT_RISK", "content": "AI-generated"},
                ],
            },
        ],
    }


class _StubServer:
    def __init__(self, root):
        self.base_dir = str(root)
        self.data_dir = str(root)
        self.sessions = {}
        self.session_store = None
        self.WORKSPACE_AVAILABLE = False
        self.workspace_manager = None
        self.logs = []

    def _save_data(self, name):
        self.saved = name

    def log_message(self, level, message, important=False):
        self.logs.append((level, message, important))


@pytest.fixture
def staging(tmp_path, monkeypatch):
    import nbot.web.routes.session_import as routes

    instance = ArchiveStaging(str(tmp_path / "staging"))
    monkeypatch.setattr(routes, "get_staging", lambda: instance)
    return instance


@pytest.fixture
def client(tmp_path, staging):
    server = _StubServer(tmp_path)
    app = Flask(__name__)
    app.config.update(TESTING=True)
    register_session_import_routes(app, server)
    with app.test_client() as test_client:
        yield test_client, server


# ----------------------------------------------------------------------------- reader


def test_index_drives_conversation_list_and_skips_missing_entries():
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 3},
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-old",
                        "title": "较早的会话",
                        "pinned": False,
                        "message_count": 2,
                        "inserted_at": "2025-12-01T00:00:00.000Z",
                        "updated_at": "2025-12-02T00:00:00.000Z",
                    },
                    {
                        "id": "session-new",
                        "title": "较新的会话",
                        "pinned": True,
                        "message_count": 1,
                        "inserted_at": "2026-01-01T00:00:00.000Z",
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    },
                    {
                        "id": "session-missing",
                        "title": "索引里有但归档里没有",
                        "message_count": 4,
                        "updated_at": "2026-01-04T00:00:00.000Z",
                    },
                    {"id": "../escape", "title": "非法 ID", "updated_at": "2026-01-05T00:00:00.000Z"},
                ]
            ),
            "chat_sessions/session-old.json": _session_json("session-old", "较早的会话"),
            "chat_sessions/session-new.json": _session_json("session-new", "较新的会话", 1, True),
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        conversations = reader.list_conversations()
        version = reader.export_format_version

    assert [item.id for item in conversations] == ["session-new", "session-old"]
    assert conversations[0].title == "较新的会话"
    assert conversations[0].pinned is True
    assert conversations[1].message_count == 2
    assert version == 3


def test_fragments_become_user_and_assistant_messages_with_reasoning_and_attachments():
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 3},
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-fragments",
                        "title": "片段示例",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-fragments.json": fragments_session(),
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        session = reader.build_session("session-fragments")

    assert session is not None
    assert session["name"] == "片段示例"
    # 普通 AI 对话没有绑定角色卡：按助手会话导入才能正常续聊
    assert session["session_mode"] == "agent"
    assert session["import_source"] == "deepseek"
    messages = session["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]

    user, assistant = messages
    assert user["content"] == "合成的用户提问\n\n📎 合成图片.png"
    assert _stamp(user["created_at"]) == _local("2026-01-02T03:04:06.000Z")
    assert assistant["content"] == "合成的助手回答"
    assert assistant["reasoning_content"] == "合成的推理过程"
    # 风险提示片段不应混进正文
    assert "AI-generated" not in assistant["content"]


def test_inverted_or_duplicated_source_timestamps_are_normalized_to_archive_order():
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 3},
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-order",
                        "title": "顺序示例",
                        "message_count": 4,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-order.json": {
                "id": "session-order",
                "title": "顺序示例",
                "inserted_at": "2026-01-02T03:04:00.000Z",
                "updated_at": "2026-01-02T03:05:00.000Z",
                "messages": [
                    {
                        "id": 1,
                        "role": "USER",
                        "inserted_at": "2026-01-02T03:04:08.417Z",
                        "fragments": [{"type": "REQUEST", "content": "合成提问"}],
                    },
                    {
                        "id": 2,
                        "role": "ASSISTANT",
                        "inserted_at": "2026-01-02T03:04:08.413Z",
                        "fragments": [{"type": "RESPONSE", "content": "合成回答"}],
                    },
                    {
                        "id": 3,
                        "role": "USER",
                        "inserted_at": "2026-01-02T03:04:08.417Z",
                        "fragments": [{"type": "REQUEST", "content": "合成追问"}],
                    },
                    {
                        "id": 4,
                        "role": "ASSISTANT",
                        "inserted_at": "2026-01-02T03:04:08.417Z",
                        "fragments": [{"type": "RESPONSE", "content": "合成再答"}],
                    },
                ],
            },
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        session = reader.build_session("session-order")

    stamps = [_stamp(message["created_at"]) for message in session["messages"]]
    assert [message["role"] for message in session["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    # 已经是递增关系的首条消息保留原始时间，重复/倒序的只做必要的毫秒级微调
    assert stamps[0] == _local("2026-01-02T03:04:08.417Z")
    assert stamps[1] == stamps[0] + timedelta(milliseconds=1)
    assert stamps[2] == stamps[1] + timedelta(milliseconds=1)
    assert stamps[3] == stamps[2] + timedelta(milliseconds=1)


def test_search_and_tool_fragments_never_leak_into_message_content():
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 3},
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-tools",
                        "title": "工具片段",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-tools.json": {
                "id": "session-tools",
                "title": "工具片段",
                "pinned": False,
                "inserted_at": "2026-01-02T00:00:00.000Z",
                "updated_at": "2026-01-02T00:00:10.000Z",
                "messages": [
                    {
                        "id": 1,
                        "role": "ASSISTANT",
                        "status": "FINISHED",
                        "inserted_at": "2026-01-02T00:00:05.000Z",
                        "fragments": [
                            {
                                "type": "TOOL_SEARCH",
                                "queries": ["合成的查询词"],
                                "results": [{"title": "合成结果", "url": "https://example.com/a"}],
                            },
                            {
                                "type": "TOOL_OPEN",
                                "status": "FINISHED",
                                "url": "https://example.com/b",
                                "title": "合成页面",
                            },
                            {"type": "RESPONSE", "content": "只有结论"},
                        ],
                    },
                    {
                        "id": 2,
                        "role": "ASSISTANT",
                        "status": "TIMEOUT",
                        "inserted_at": "2026-01-02T00:00:06.000Z",
                        "fragments": [{"type": "THINK", "content": "只有推理，没有正文"}],
                    },
                ],
            },
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        session = reader.build_session("session-tools")

    assert len(session["messages"]) == 1
    assert session["messages"][0]["content"] == "只有结论"
    assert "reasoning_content" not in session["messages"][0]


def test_conversation_without_readable_messages_is_skipped():
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-empty",
                        "title": "空会话",
                        "message_count": 0,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-empty.json": {
                "id": "session-empty",
                "title": "空会话",
                "messages": [
                    {"id": 1, "role": "USER", "fragments": []},
                    {
                        "id": 2,
                        "role": "ASSISTANT",
                        "fragments": [{"type": "CONTENT_RISK", "content": "提示"}],
                    },
                ],
            },
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        assert reader.build_session("session-empty") is None


def test_wrapped_single_root_directory_is_supported():
    blob = _archive(
        {
            "deepseek_data-20260101/manifest.json": {"export_format_version": 3},
            "deepseek_data-20260101/chat_session_index.json": _index(
                [
                    {
                        "id": "session-wrapped",
                        "title": "打包目录",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "deepseek_data-20260101/chat_sessions/session-wrapped.json": _session_json(
                "session-wrapped", "打包目录"
            ),
        }
    )

    with DeepSeekArchiveReader.open(blob) as reader:
        conversations = reader.list_conversations()
        session = reader.build_session("session-wrapped")

    assert [item.id for item in conversations] == ["session-wrapped"]
    assert session is not None
    assert len(session["messages"]) == 2


def test_archive_without_session_data_is_rejected():
    blob = _archive({"readme.txt": "不是导出包"})
    with pytest.raises(SessionImportError) as excinfo:
        DeepSeekArchiveReader.open(blob)
    assert "deepseek" in str(excinfo.value)


def test_unsupported_format_version_is_rejected():
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 99},
            "chat_session_index.json": _index([]),
            "chat_sessions/session-x.json": _session_json("session-x", "版本过新"),
        }
    )
    with pytest.raises(SessionImportError) as excinfo:
        DeepSeekArchiveReader.open(blob)
    assert "99" in str(excinfo.value)


def test_non_zip_file_is_rejected():
    with pytest.raises(SessionImportError):
        DeepSeekArchiveReader.open(b"not a zip archive")


def test_path_traversal_conversation_ids_are_ignored():
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [{"id": "../secrets", "title": "越界", "updated_at": "2026-01-01T00:00:00.000Z"}]
            ),
            "chat_sessions/../secrets.json": _session_json("../secrets", "越界"),
        }
    )
    with DeepSeekArchiveReader.open(blob) as reader:
        assert reader.list_conversations() == []
        assert reader.build_session("../secrets") is None


def test_oversized_session_entry_is_rejected(monkeypatch):
    monkeypatch.setattr(deepseek_module, "_MAX_SESSION_BYTES", 32)
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-big",
                        "title": "超大",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-big.json": _session_json("session-big", "超大"),
        }
    )
    with DeepSeekArchiveReader.open(blob) as reader:
        with pytest.raises(SessionImportError):
            reader.build_session("session-big")


# ----------------------------------------------------------------------------- payload


def test_normalize_imported_session_keeps_export_metadata():
    session = {
        "name": "片段示例",
        "session_mode": "agent",
        "pinned": True,
        "created_at": "2026-01-02T03:04:05.000",
        "updated_at": "2026-01-02T03:04:07.000",
        "messages": [{"role": "user", "content": "合成提问"}],
    }
    session_id, normalized = normalize_imported_session(session)

    assert session_id != session.get("id")
    assert normalized["name"] == "片段示例"
    assert normalized["session_mode"] == "agent"
    assert normalized["pinned"] is True
    assert normalized["created_at"] == "2026-01-02T03:04:05.000"
    assert normalized["updated_at"] == "2026-01-02T03:04:07.000"
    assert normalized["type"] == "web"
    assert normalized["imported_at"]


def test_extract_export_sessions_supports_known_payload_shapes():
    assert extract_export_sessions({"sessions": [{"name": "a"}]}) == [{"name": "a"}]
    assert extract_export_sessions({"session": {"name": "b"}}) == [{"name": "b"}]
    assert extract_export_sessions({"name": "c", "messages": []}) == [{"name": "c", "messages": []}]
    assert extract_export_sessions({"type": "nbot_session_export", "version": 1}) is None
    assert extract_export_sessions("not json") is None


# ----------------------------------------------------------------------------- routes


def test_formats_route_lists_registered_formats(client):
    test_client, _ = client
    response = test_client.get("/api/sessions/import/formats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert [item["id"] for item in payload["formats"]] == ["nekobot", "deepseek"]
    assert payload["formats"] == list_session_import_formats()


def test_import_routes_take_precedence_over_session_id_rule(tmp_path):
    """静态路径不能被 /api/sessions/<session_id> 抢走。"""
    from nbot.web.routes.sessions import register_session_routes

    server = _StubServer(tmp_path)
    app = Flask(__name__)
    app.config.update(TESTING=True)
    register_session_routes(app, server)
    register_session_import_routes(app, server)

    with app.test_client() as test_client:
        response = test_client.get("/api/sessions/import/formats")

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_scan_and_import_round_trip(client):
    test_client, server = client
    blob = _archive(
        {
            "manifest.json": {"export_format_version": 3},
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-fragments",
                        "title": "片段示例",
                        "pinned": True,
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-fragments.json": {
                **fragments_session(),
                "pinned": True,
            },
        }
    )

    scan = test_client.post(
        "/api/sessions/import/deepseek/scan",
        data={"file": (io.BytesIO(blob), "deepseek_data-demo.zip")},
        content_type="multipart/form-data",
    )
    assert scan.status_code == 200, scan.get_json()
    scan_payload = scan.get_json()
    assert scan_payload["success"] is True
    assert scan_payload["format_version"] == 3
    assert scan_payload["total"] == 1
    assert scan_payload["conversations"][0]["title"] == "片段示例"
    token = scan_payload["token"]

    imported = test_client.post(
        "/api/sessions/import/deepseek/import",
        json={"token": token, "ids": ["session-fragments"]},
    )
    assert imported.status_code == 200, imported.get_json()
    result = imported.get_json()
    assert result["imported"] == 1
    assert result["failed"] == 0
    assert result["skipped"] == 0

    session_id = result["sessions"][0]["id"]
    stored = server.sessions[session_id]
    assert stored["name"] == "片段示例"
    assert stored["session_mode"] == "agent"
    assert stored["pinned"] is True
    assert stored["type"] == "web"
    assert stored["imported_from"] == "session-fragments"
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert server.logs  # 导入完成会写一条系统日志


def test_scan_rejects_invalid_archive(client):
    test_client, _ = client
    response = test_client.post(
        "/api/sessions/import/deepseek/scan",
        data={"file": (io.BytesIO(b"not a zip"), "random.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_scan_without_file_or_url_is_rejected(client):
    test_client, _ = client
    response = test_client.post("/api/sessions/import/deepseek/scan", json={})
    assert response.status_code == 400


def test_import_requires_known_token(client):
    test_client, _ = client
    response = test_client.post(
        "/api/sessions/import/deepseek/import",
        json={"token": "missing", "ids": ["a"]},
    )
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_import_skips_conversations_without_readable_messages(client):
    test_client, server = client
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-empty",
                        "title": "空会话",
                        "message_count": 0,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    },
                    {
                        "id": "session-ok",
                        "title": "有正文",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    },
                ]
            ),
            "chat_sessions/session-empty.json": {
                "id": "session-empty",
                "title": "空会话",
                "messages": [{"id": 1, "role": "USER", "fragments": []}],
            },
            "chat_sessions/session-ok.json": _session_json("session-ok", "有正文"),
        }
    )
    scan = test_client.post(
        "/api/sessions/import/deepseek/scan",
        data={"file": (io.BytesIO(blob), "deepseek_data-demo.zip")},
        content_type="multipart/form-data",
    )
    token = scan.get_json()["token"]

    response = test_client.post(
        "/api/sessions/import/deepseek/import",
        json={"token": token, "ids": ["session-empty", "session-ok"]},
    )

    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert len(server.sessions) == 1


def test_discard_removes_staged_archive(client, staging):
    test_client, _ = client
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-ok",
                        "title": "有正文",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-ok.json": _session_json("session-ok", "有正文"),
        }
    )
    scan = test_client.post(
        "/api/sessions/import/deepseek/scan",
        data={"file": (io.BytesIO(blob), "deepseek_data-demo.zip")},
        content_type="multipart/form-data",
    )
    token = scan.get_json()["token"]

    assert test_client.post(
        "/api/sessions/import/deepseek/discard", json={"token": token}
    ).status_code == 200

    follow_up = test_client.post(
        "/api/sessions/import/deepseek/import",
        json={"token": token, "ids": ["session-ok"]},
    )
    assert follow_up.status_code == 400


def test_oversized_upload_is_rejected(tmp_path, monkeypatch):
    import nbot.web.routes.session_import as routes

    instance = ArchiveStaging(str(tmp_path / "staging"), max_bytes=16)
    monkeypatch.setattr(routes, "get_staging", lambda: instance)

    server = _StubServer(tmp_path)
    app = Flask(__name__)
    app.config.update(TESTING=True)
    register_session_import_routes(app, server)

    with app.test_client() as test_client:
        response = test_client.post(
            "/api/sessions/import/deepseek/scan",
            data={"file": (io.BytesIO(b"x" * 64), "big.zip")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


# ----------------------------------------------------------------------------- downloader


def test_require_public_https_url_rejects_internal_targets():
    for url in (
        "http://example.com/a.zip",
        "https://127.0.0.1/a.zip",
        "https://localhost/a.zip",
        "https://[::1]/a.zip",
        "not-a-url",
    ):
        with pytest.raises(SessionImportError):
            require_public_https_url(url)


def test_stage_download_uses_link_basename(tmp_path):
    instance = ArchiveStaging(str(tmp_path / "staging"))
    blob = _archive(
        {
            "chat_session_index.json": _index(
                [
                    {
                        "id": "session-link",
                        "title": "链接导入",
                        "message_count": 2,
                        "updated_at": "2026-01-03T00:00:00.000Z",
                    }
                ]
            ),
            "chat_sessions/session-link.json": _session_json("session-link", "链接导入"),
        }
    )

    def fake_download(url, target, *, max_bytes):
        with open(target, "wb") as handle:
            handle.write(blob)
        return len(blob)

    staged = instance.stage_download(
        "https://example.com/exports/deepseek_data-demo.zip",
        downloader=fake_download,
    )

    assert staged.name == "deepseek_data-demo.zip"
    assert staged.size == len(blob)
    assert instance.get(staged.token).path == staged.path
