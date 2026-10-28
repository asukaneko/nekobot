"""P0-P2 审查发现的问题：凭据、导入冲突、宏、注入顺序与冷却。"""

import io
import json
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

from nbot.character.models import WorldBookEntry
from nbot.character.runtime import CharacterRuntime
from nbot.character.world_book_state import WorldBookStateStore
from nbot.core.ai_pipeline import AIPipeline, PipelineContext
from nbot.core.chat_models import ChatRequest
from nbot.core.macros import MacroContext, expand
from nbot.core.prompt_manager import apply_preset_base, apply_preset_messages
from nbot.core.session_store import WebSessionStore
from nbot.web.routes import export_center as ec
from nbot.web import sessions_db


def test_settings_zip_excludes_nested_credentials_and_retains_limits(tmp_path):
    settings = {
        "theme": "dark", "max_tokens": 8192,
        "webdav_backup": {"url": "https://example.com", "password": "dav-secret",
                          "encryption_password": "encryption-secret"},
        "realtime_voice": {"api_key": "voice-secret"},
        "items": [{"access_token": "access-secret", "name": "public"}],
    }
    server = SimpleNamespace(data_dir=str(tmp_path), settings=settings)
    with zipfile.ZipFile(io.BytesIO(ec.build_export_zip(server, ["settings"]))) as archive:
        payload = json.loads(archive.read("data/settings.json"))
    assert payload["webdav_backup"] == {"url": "https://example.com"}
    assert payload["realtime_voice"] == {}
    assert payload["items"] == [{"name": "public"}]
    assert payload["max_tokens"] == 8192
    assert settings["webdav_backup"]["password"] == "dav-secret"


@pytest.mark.parametrize("new_url,keep_password", [
    ("https://old.example", True), ("https://new.example", False),
])
def test_settings_overwrite_keeps_only_credentials_for_same_destination(
    tmp_path, new_url, keep_password
):
    server = SimpleNamespace(data_dir=str(tmp_path), settings={
        "webdav_backup": {"url": "https://old.example", "password": "local-secret"},
    })
    ec._import_settings(server, {
        "webdav_backup": {"url": new_url, "password": "untrusted-secret"},
    }, "overwrite")
    restored = server.settings["webdav_backup"]
    assert (restored.get("password") == "local-secret") is keep_password
    assert "untrusted-secret" not in json.dumps(server.settings)


def test_merge_import_skips_unloaded_disk_session(tmp_path):
    existing = {"id": "disk-only", "messages": [{"role": "user", "content": "keep"}]}
    sessions_db.upsert_session(str(tmp_path), "disk-only", existing)
    server = SimpleNamespace(data_dir=str(tmp_path), base_dir=str(tmp_path), sessions={})
    stats = ec._import_sessions(server, [{"id": "disk-only", "messages": []}], "merge")
    assert stats == {"added": 0, "updated": 0, "skipped": 1}
    assert sessions_db.get_session(str(tmp_path), "disk-only") == existing


def test_overwrite_import_updates_unloaded_disk_session(tmp_path):
    sessions_db.upsert_session(str(tmp_path), "disk-only", {"id": "disk-only", "name": "old"})
    server = SimpleNamespace(data_dir=str(tmp_path), base_dir=str(tmp_path), sessions={})
    stats = ec._import_sessions(server, [{"id": "disk-only", "name": "new"}], "overwrite")
    assert stats == {"added": 0, "updated": 1, "skipped": 0}
    assert sessions_db.get_session(str(tmp_path), "disk-only")["name"] == "new"


def test_import_preserves_other_unloaded_sessions_when_save_rewrites_table(tmp_path):
    original = {"id": "disk-only", "messages": [{"role": "user", "content": "keep"}]}
    sessions_db.upsert_session(str(tmp_path), "disk-only", original)
    sessions = {}
    store = WebSessionStore(sessions, lambda: sessions_db.save_sessions(str(tmp_path), sessions))
    server = SimpleNamespace(data_dir=str(tmp_path), base_dir=str(tmp_path),
                             sessions=sessions, session_store=store)
    stats = ec._import_sessions(server, [{"id": "new", "messages": []}], "merge")
    assert stats["added"] == 1
    assert sessions_db.get_session(str(tmp_path), "disk-only") == original


def test_pipeline_executes_preset_system_macro_once():
    pipeline = AIPipeline()
    ctx = PipelineContext(chat_request=ChatRequest(
        channel="web", conversation_id="isolated", content="hello"
    ))
    ctx.metadata = {"macro_variables": {"counter": 0}}
    preset = {"sections": [{"id": "counter", "role": "system", "position": "end",
                            "content": "{{addvar::counter::1}}n={{getvar::counter}}"}]}
    rendered = pipeline._expand_prompt_macros(ctx, apply_preset_base(preset, "base"))
    result = pipeline._apply_prompt_preset(ctx, preset, [
        {"role": "system", "content": rendered}, {"role": "user", "content": "hello"},
    ])
    assert ctx.metadata["macro_variables"]["counter"] == 1
    assert result[0]["content"] == "base\n\nn=1"


def test_preset_helpers_expand_only_sections_they_apply():
    context = MacroContext(variables={"counter": 0})
    preset = {"sections": [
        {"id": "system", "role": "system", "content": "{{addvar::counter::1}}system"},
        {"id": "user", "role": "user", "content": "{{addvar::counter::1}}user"},
    ]}
    expander = lambda text: expand(text, context)
    system = apply_preset_base(preset, "base", expander=expander)
    assert context.variables["counter"] == 1
    messages = apply_preset_messages(preset, [{"role": "system", "content": system}],
                                     expander=expander)
    assert context.variables["counter"] == 2
    assert messages[-1]["content"] == "user"


def test_same_depth_sections_keep_configured_order_and_original_anchor():
    preset = {"sections": [
        {"id": "a", "order": 0, "position": "depth", "depth": 1, "content": "A"},
        {"id": "b", "order": 1, "position": "depth", "depth": 1, "content": "B"},
        {"id": "c", "order": 2, "position": "depth", "depth": 2, "content": "C"},
    ]}
    messages = [{"role": "system", "content": "base"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}]
    result = apply_preset_messages(preset, messages)
    assert [m["content"] for m in result] == ["base", "u1", "C", "a1", "A", "B", "u2"]


def test_world_book_cooldown_counts_turns_without_matches(tmp_path):
    state = WorldBookStateStore(str(tmp_path / "world_book_state.json"))
    identity = SimpleNamespace(character_id="Alice", scope_id="isolated")
    entry = WorldBookEntry(id="entry", content="test", cooldown_turns=2)
    matched = SimpleNamespace(entry=entry, trigger_sources=["keyword"])
    with patch("nbot.character.world_book_state.get_world_book_state_store", return_value=state):
        apply = CharacterRuntime._apply_world_book_injection_policy
        assert apply(None, identity, [matched]) == [matched]
        assert apply(None, identity, []) == []
        assert apply(None, identity, []) == []
        assert apply(None, identity, [matched]) == [matched]
