"""
角色管理 API 路由

提供角色列表、详情、状态、关系、记忆、调试等接口。
保留旧 /api/personality 接口不变，新增 /api/characters 系列接口。
"""

import logging
import os
from typing import Any

from flask import jsonify, request

_log = logging.getLogger(__name__)


def _get_character_runtime(server):
    """获取 CharacterRuntime 实例"""
    return getattr(server, "character_runtime", None)


def _get_base_dir(server):
    """获取项目根目录"""
    return getattr(server, "base_dir", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _get_profile_initial_state(server, character_id: str) -> dict[str, Any]:
    from nbot.character.repository import ProfileRepository

    profile = ProfileRepository(_get_base_dir(server)).get(character_id)
    return profile.initial_state if profile else {}


_HISTORY_STORES: dict[str, Any] = {}


def _get_history_store(server):
    """按 base_dir 缓存状态快照存储实例。"""
    from nbot.character.state_history import StateHistoryStore

    base_dir = _get_base_dir(server)
    store = _HISTORY_STORES.get(base_dir)
    if store is None:
        store = StateHistoryStore(base_dir)
        _HISTORY_STORES[base_dir] = store
    return store


def _record_state_history(server, character_id, scope_id, state, relationship,
                          source="manual", reason=""):
    """记录一条状态快照；失败只记日志，不影响接口返回。"""
    try:
        _get_history_store(server).record(
            character_id, scope_id, state, relationship, source=source, reason=reason
        )
    except Exception as exc:
        _log.debug("[StateHistory] 写入失败: %s", exc)


def from_state_history_diff(before, after):
    """包装 snapshot_diff，保持路由层命名清晰。"""
    from nbot.character.state_history import snapshot_diff

    return {
        "from": str((before or {}).get("ts") or ""),
        "to": str((after or {}).get("ts") or ""),
        "changes": snapshot_diff(before or {}, after or {}),
    }


def _channel_from_scope_id(scope_id: str) -> str:
    scope_id = str(scope_id or "").strip()
    return scope_id.split(":", 1)[0] if ":" in scope_id else "unknown"


def _target_candidates_from_scope_id(scope_id: str) -> list[str]:
    scope_id = str(scope_id or "").strip()
    candidates = [scope_id] if scope_id else []
    parts = scope_id.split(":")

    if len(parts) >= 3 and parts[0] == "qq" and parts[1] == "user":
        candidates.append(parts[2])
    if parts and parts[0] == "qq" and "user" in parts:
        user_index = len(parts) - 1 - list(reversed(parts)).index("user")
        if user_index + 1 < len(parts):
            candidates.append(parts[user_index + 1])
    if len(parts) >= 2 and parts[0] == "web":
        candidates.append(scope_id)

    seen = set()
    result = []
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _channel_session_name(
    channel: str,
    scope_id: str,
    target_id: str,
    character_id: str,
) -> str:
    label = {
        "qq": "QQ",
        "telegram": "Telegram",
        "feishu": "Feishu",
        "web": "Web",
    }.get(channel, channel or "unknown")
    if channel == "qq" and scope_id.startswith("qq:user:"):
        label = f"{label} 私聊 {target_id or scope_id.rsplit(':', 1)[-1]}"
    else:
        label = f"{label} {target_id or scope_id}"
    return f"{label} · {character_id or '角色'}"


def register_character_routes(app, server):
    """注册角色管理 API 路由"""

    # ================================================================
    # 角色列表 / 详情 / 创建 / 更新 / 删除
    # ================================================================

    @app.route("/api/characters", methods=["GET"])
    def list_characters():
        """列出所有角色卡"""
        from nbot.character.repository import ProfileRepository
        repo = ProfileRepository(_get_base_dir(server))
        profiles = repo.list_all()
        return jsonify([p.to_personality_dict() for p in profiles])

    @app.route("/api/characters/<character_id>", methods=["GET"])
    def get_character(character_id):
        """获取角色卡详情"""
        from nbot.character.repository import ProfileRepository
        repo = ProfileRepository(_get_base_dir(server))
        profile = repo.get(character_id)
        if not profile:
            return jsonify({"success": False, "error": "角色不存在"}), 404
        return jsonify(profile.to_personality_dict())

    @app.route("/api/characters", methods=["POST"])
    def create_character():
        """创建角色卡"""
        data = request.json or {}
        from nbot.character.compiler import compile_profile_prompt
        from nbot.character.models import CharacterProfile
        from nbot.character.repository import ProfileRepository

        profile = CharacterProfile.from_personality_dict(data)
        if not profile.id:
            import uuid
            profile.id = str(uuid.uuid4())

        profile.system_prompt = compile_profile_prompt(profile)

        repo = ProfileRepository(_get_base_dir(server))
        repo.save(profile)
        return jsonify({"success": True, "character": profile.to_personality_dict()})

    @app.route("/api/characters/<character_id>", methods=["PUT"])
    def update_character(character_id):
        """更新角色卡"""
        data = request.json or {}
        from nbot.character.compiler import compile_profile_prompt
        from nbot.character.models import CharacterProfile
        from nbot.character.repository import ProfileRepository

        repo = ProfileRepository(_get_base_dir(server))
        existing = repo.get(character_id)
        if not existing:
            return jsonify({"success": False, "error": "角色不存在"}), 404

        profile = CharacterProfile.from_personality_dict(data)
        profile.id = character_id
        profile.system_prompt = compile_profile_prompt(profile)

        repo.save(profile)
        return jsonify({"success": True, "character": profile.to_personality_dict()})

    @app.route("/api/characters/<character_id>", methods=["DELETE"])
    def delete_character(character_id):
        """删除角色卡"""
        from nbot.character.repository import ProfileRepository
        repo = ProfileRepository(_get_base_dir(server))
        if repo.delete(character_id):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "角色不存在"}), 404

    # ================================================================
    # 角色状态
    # ================================================================

    @app.route("/api/characters/<character_id>/state", methods=["GET"])
    def get_character_state(character_id):
        """获取角色运行时状态"""
        scope_id = request.args.get("scope_id", "")
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id 参数"}), 400

        from nbot.character.repository import CharacterStateRepository
        repo = CharacterStateRepository(_get_base_dir(server))
        state = repo.get(character_id, scope_id)
        if not state:
            return jsonify({"success": False, "error": "状态不存在"}), 404
        return jsonify(state.to_dict())

    @app.route("/api/characters/<character_id>/state", methods=["PUT"])
    def update_character_state(character_id):
        """手动更新角色运行时状态"""
        scope_id = request.args.get("scope_id", "")
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id 参数"}), 400

        data = request.json or {}
        from nbot.character.models import CharacterState
        from nbot.character.repository import CharacterStateRepository

        repo = CharacterStateRepository(_get_base_dir(server))
        state = repo.get(character_id, scope_id)
        if not state:
            state = CharacterState(character_id=character_id, scope_id=scope_id)

        # 更新字段
        if "mood" in data:
            state.mood = data["mood"]
        if "mood_intensity" in data:
            state.mood_intensity = float(data["mood_intensity"])
        if "energy" in data:
            state.energy = int(data["energy"])

        repo.save(state)
        _record_state_history(server, character_id, scope_id, state, None, source="manual",
                              reason="手动修改状态")
        return jsonify({"success": True, "state": state.to_dict()})

    # ================================================================
    # 关系状态
    # ================================================================

    @app.route("/api/characters/<character_id>/relationships", methods=["GET"])
    def get_character_relationship(character_id):
        """获取角色与用户的关系状态"""
        target_id = request.args.get("target_id", "")
        if not target_id:
            return jsonify({"success": False, "error": "缺少 target_id 参数"}), 400

        from nbot.character.repository import RelationshipRepository
        repo = RelationshipRepository(_get_base_dir(server))
        rel = repo.get_or_create(
            character_id,
            target_id,
            initial_state=_get_profile_initial_state(server, character_id),
        )
        if not rel:
            return jsonify({"success": False, "error": "关系不存在"}), 404
        return jsonify(rel.to_dict())

    @app.route("/api/characters/<character_id>/relationships", methods=["PUT"])
    def update_character_relationship(character_id):
        """手动更新关系状态"""
        target_id = request.args.get("target_id", "")
        if not target_id:
            return jsonify({"success": False, "error": "缺少 target_id 参数"}), 400

        data = request.json or {}
        from nbot.character.repository import RelationshipRepository

        repo = RelationshipRepository(_get_base_dir(server))
        rel = repo.get_or_create(
            character_id,
            target_id,
            initial_state=_get_profile_initial_state(server, character_id),
        )

        # 更新字段
        for field_name in ["affection", "trust", "familiarity", "dependency", "security", "jealousy"]:
            if field_name in data:
                value = int(data[field_name])
                value = max(0, min(100, value))
                setattr(rel, field_name, value)

        repo.save(rel)
        _record_state_history(server, character_id, target_id, None, rel, source="manual",
                              reason="手动修改关系")
        return jsonify({"success": True, "relationship": rel.to_dict()})

    # ================================================================
    # 状态快照历史（时间线 / 趋势 / 恢复）
    # ================================================================

    @app.route("/api/characters/<character_id>/state/history", methods=["GET"])
    def get_character_state_history(character_id):
        """按 scope 返回状态快照时间线（升序）。"""
        scope_id = request.args.get("scope_id", "").strip()
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id 参数"}), 400
        limit = request.args.get("limit", "50")
        try:
            limit = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            limit = 50

        store = _get_history_store(server)
        snapshots = store.list_snapshots(character_id, scope_id, limit=limit)
        diffs = []
        for prev, cur in zip(snapshots, snapshots[1:], strict=False):
            diffs.append(from_state_history_diff(prev, cur))

        return jsonify({
            "success": True,
            "character_id": character_id,
            "scope_id": scope_id,
            "count": len(snapshots),
            "snapshots": snapshots,
            "diffs": diffs,
        })

    @app.route("/api/characters/<character_id>/state/history/scopes", methods=["GET"])
    def list_character_state_history_scopes(character_id):
        """列出该角色有历史记录的会话范围。"""
        store = _get_history_store(server)
        return jsonify({"success": True, "scopes": store.list_scopes(character_id)})

    @app.route("/api/characters/<character_id>/state/trend", methods=["GET"])
    def get_character_state_trend(character_id):
        """返回趋势序列，供折线图使用。"""
        scope_id = request.args.get("scope_id", "").strip()
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id 参数"}), 400
        try:
            limit = max(2, min(500, int(request.args.get("limit", "100"))))
        except (TypeError, ValueError):
            limit = 100
        raw_fields = request.args.get("fields", "")
        fields = [f for f in raw_fields.split(",") if f.strip()] or None

        store = _get_history_store(server)
        payload = store.trend(character_id, scope_id, limit=limit, fields=fields)
        payload.update({"success": True, "character_id": character_id, "scope_id": scope_id})
        return jsonify(payload)

    @app.route("/api/characters/<character_id>/state/restore", methods=["POST"])
    def restore_character_state(character_id):
        """把状态（可选关系）恢复到指定快照。"""
        from nbot.character.repository import CharacterStateRepository, RelationshipRepository

        data = request.json or {}
        scope_id = str(data.get("scope_id") or "").strip()
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id"}), 400

        store = _get_history_store(server)
        snapshots = store.list_snapshots(character_id, scope_id, limit=0)
        if not snapshots:
            return jsonify({"success": False, "error": "该会话没有状态快照"}), 404

        index = data.get("index")
        if index is None:
            index = len(snapshots) - 1
        try:
            index = int(index)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "index 必须为整数"}), 400
        if index < 0:
            index += len(snapshots)
        if not 0 <= index < len(snapshots):
            return jsonify({"success": False, "error": "index 超出范围"}), 400

        snapshot = snapshots[index]
        base_dir = _get_base_dir(server)

        state_repo = CharacterStateRepository(base_dir)
        state = state_repo.get(character_id, scope_id)
        if not state:
            from nbot.character.models import CharacterState

            state = CharacterState(character_id=character_id, scope_id=scope_id)
        if "mood" in snapshot:
            state.mood = str(snapshot["mood"])
        if "mood_intensity" in snapshot:
            state.mood_intensity = float(snapshot["mood_intensity"])
        if "energy" in snapshot:
            state.energy = int(snapshot["energy"])
        state_repo.save(state)

        relationship_payload = None
        target_id = str(data.get("target_id") or "").strip()
        if not target_id:
            candidates = _target_candidates_from_scope_id(scope_id)
            target_id = candidates[0] if candidates else ""
        if target_id:
            rel_repo = RelationshipRepository(base_dir)
            rel = rel_repo.get(character_id, target_id)
            if rel is None:
                rel = rel_repo.get_or_create(
                    character_id,
                    target_id,
                    initial_state=_get_profile_initial_state(server, character_id),
                )
            for field_name in ("affection", "trust", "familiarity", "dependency",
                               "security", "jealousy"):
                if field_name in snapshot:
                    setattr(rel, field_name, max(0, min(100, int(snapshot[field_name]))))
            rel_repo.save(rel)
            relationship_payload = rel.to_dict()

        _record_state_history(server, character_id, scope_id, state, rel if target_id else None,
                              source="restore",
                              reason=f"恢复到快照 {snapshot.get('ts', '')}")

        return jsonify({
            "success": True,
            "restored": {
                "state": state.to_dict(),
                "relationship": relationship_payload,
                "snapshot": snapshot,
                "index": index,
            },
        })

    # ================================================================
    # 记忆管理
    # ================================================================

    @app.route("/api/characters/<character_id>/memories", methods=["GET"])
    def list_character_memories(character_id):
        """列出角色的记忆"""
        target_id = request.args.get("target_id", "")
        from nbot.character.memory import PromptManagerMemoryAdapter
        adapter = PromptManagerMemoryAdapter()
        memories = adapter.search(
            character_id=character_id,
            target_id=target_id,
            limit=50,
        )
        return jsonify([m.to_dict() for m in memories])

    @app.route("/api/characters/<character_id>/memories", methods=["POST"])
    def add_character_memory(character_id):
        """手动添加角色记忆"""
        data = request.json or {}
        target_id = data.get("target_id", "")
        title = data.get("title", "")
        content = data.get("content", "")
        mem_type = data.get("type", "long")

        if not title or not content:
            return jsonify({"success": False, "error": "标题和内容不能为空"}), 400

        from nbot.character.memory import PromptManagerMemoryAdapter
        adapter = PromptManagerMemoryAdapter()
        if adapter.save(character_id, target_id, title, content, mem_type=mem_type):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "保存失败"}), 500

    @app.route("/api/characters/<character_id>/memories/<memory_id>", methods=["DELETE"])
    def delete_character_memory(character_id, memory_id):
        """删除角色记忆"""
        from nbot.character.memory import PromptManagerMemoryAdapter
        adapter = PromptManagerMemoryAdapter()
        if adapter.delete(memory_id):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "删除失败"}), 500

    # ================================================================
    # 多频道数据查看
    # ================================================================

    def _build_channel_states(character_id: str, channel_filter: str = "") -> dict[str, Any]:
        """构建频道状态数据的公共逻辑"""
        from nbot.character.repository import CharacterStateRepository, RelationshipRepository

        base_dir = _get_base_dir(server)
        state_repo = CharacterStateRepository(base_dir)
        rel_repo = RelationshipRepository(base_dir)

        states = state_repo.list_by_character(character_id)
        relationships = rel_repo.list_by_character(character_id)

        grouped: dict[str, list] = {}
        for s in states:
            sid = s.scope_id or ""
            channel = sid.split(":")[0] if ":" in sid else "unknown"
            if channel_filter and channel != channel_filter:
                continue
            entry = {
                "scope_id": sid,
                "character_id": s.character_id,
                "mood": s.mood,
                "mood_intensity": s.mood_intensity,
                "energy": s.energy,
            }
            grouped.setdefault(channel, []).append(entry)

        rel_list = []
        for r in relationships:
            rel_list.append({
                "target_id": r.target_id,
                "character_id": r.character_id,
                "affection": r.affection,
                "trust": r.trust,
                "familiarity": r.familiarity,
                "dependency": r.dependency,
                "security": r.security,
                "jealousy": r.jealousy,
            })

        return {
            "states": grouped,
            "relationships": rel_list,
            "channels": sorted(grouped.keys()),
        }

    @app.route("/api/channel_states", methods=["GET"])
    def list_all_channel_states():
        """列出所有角色在各频道的状态数据（直接遍历存储，不依赖 profile 匹配）"""
        from nbot.character.storage.json_store import CharacterStateJsonStore, RelationshipJsonStore

        channel_filter = request.args.get("channel", "").strip().lower()
        base_dir = _get_base_dir(server)

        state_store = CharacterStateJsonStore(base_dir)
        rel_store = RelationshipJsonStore(base_dir)

        all_states: dict[str, list] = {}
        all_channels = set()

        for item in state_store.list_all():
            sid = item.get("scope_id", "")
            channel = sid.split(":")[0] if ":" in sid else "unknown"
            if channel_filter and channel != channel_filter:
                continue
            entry = {
                "scope_id": sid,
                "character_id": item.get("character_id", ""),
                "mood": item.get("mood", ""),
                "mood_intensity": item.get("mood_intensity", 0),
                "energy": item.get("energy", 0),
            }
            all_states.setdefault(channel, []).append(entry)
            all_channels.add(channel)

        all_relationships = []
        for item in rel_store.list_all():
            all_relationships.append({
                "target_id": item.get("target_id", ""),
                "character_id": item.get("character_id", ""),
                "affection": item.get("affection", 50),
                "trust": item.get("trust", 50),
                "familiarity": item.get("familiarity", 30),
                "dependency": item.get("dependency", 30),
                "security": item.get("security", 50),
                "jealousy": item.get("jealousy", 0),
            })

        return jsonify({
            "states": all_states,
            "relationships": all_relationships,
            "channels": sorted(all_channels),
        })

    @app.route("/api/channel_runtime_timeline", methods=["GET"])
    def list_channel_runtime_timeline():
        """Return non-Web channel runtime states as synthetic timeline sessions."""
        from nbot.character.storage.json_store import (
            CharacterStateJsonStore,
            RelationshipJsonStore,
        )

        channel_filter = request.args.get("channel", "").strip().lower()
        character_filter = request.args.get("character_id", "").strip()
        base_dir = _get_base_dir(server)

        state_store = CharacterStateJsonStore(base_dir)
        rel_store = RelationshipJsonStore(base_dir)

        rel_index = {}
        for rel in rel_store.list_all():
            if not isinstance(rel, dict):
                continue
            rel_index[
                (
                    str(rel.get("character_id") or "").strip(),
                    str(rel.get("target_id") or "").strip(),
                )
            ] = rel

        sessions = []
        channels = set()
        for state in state_store.list_all():
            if not isinstance(state, dict):
                continue

            scope_id = str(state.get("scope_id") or "").strip()
            character_id = str(state.get("character_id") or "").strip()
            if not scope_id or not character_id:
                continue

            channel = _channel_from_scope_id(scope_id)
            if channel == "web":
                continue
            if channel_filter and channel != channel_filter:
                continue
            if character_filter and character_id != character_filter:
                continue

            channels.add(channel)
            target_candidates = _target_candidates_from_scope_id(scope_id)
            relationship = {}
            target_id = target_candidates[0] if target_candidates else scope_id
            for candidate in target_candidates:
                found = rel_index.get((character_id, candidate))
                if found:
                    relationship = found
                    target_id = candidate
                    break

            timestamp = (
                state.get("updated_at")
                or state.get("last_active_at")
                or relationship.get("updated_at")
                or ""
            )
            snapshot = {
                "character_id": character_id,
                "channel": channel,
                "scope_id": scope_id,
                "target_id": target_id,
                "mood": state.get("mood", ""),
                "mood_intensity": state.get("mood_intensity", 0),
                "energy": state.get("energy", 0),
                "affection": relationship.get("affection", 50),
                "trust": relationship.get("trust", 50),
                "familiarity": relationship.get("familiarity", 30),
                "dependency": relationship.get("dependency", 30),
                "security": relationship.get("security", 50),
                "jealousy": relationship.get("jealousy", 0),
                "timestamp": timestamp,
            }
            sessions.append({
                "id": f"channel:{channel}:{character_id}:{scope_id}",
                "type": "channel",
                "channel": channel,
                "name": _channel_session_name(channel, scope_id, target_id, character_id),
                "character_id": character_id,
                "sender_name": character_id,
                "scope_id": scope_id,
                "target_id": target_id,
                "updated_at": timestamp,
                "created_at": timestamp,
                "archived": False,
                "character_runtime_snapshot": snapshot,
                "character_runtime_timeline": [snapshot],
            })

        sessions.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return jsonify({
            "success": True,
            "sessions": sessions,
            "channels": sorted(channels),
        })

    @app.route("/api/characters/<character_id>/channel_states", methods=["GET"])
    def list_character_channel_states(character_id):
        """列出指定角色在各频道的状态数据，按频道分组

        可选参数：
        - channel: 只返回指定频道的数据（如 ?channel=qq）
        """
        channel_filter = request.args.get("channel", "").strip().lower()
        return jsonify(_build_channel_states(character_id, channel_filter))

    # ================================================================
    # 调试接口
    # ================================================================

    @app.route("/api/characters/<character_id>/debug/latest-turn", methods=["GET"])
    def get_character_debug_latest(character_id):
        """获取最近一轮的调试快照"""
        scope_id = request.args.get("scope_id", "")
        if not scope_id:
            return jsonify({"success": False, "error": "缺少 scope_id 参数"}), 400

        from nbot.character.events import CharacterEventLogger
        logger = CharacterEventLogger(_get_base_dir(server))
        snapshot = logger.get_latest_debug_snapshot(scope_id)
        if not snapshot:
            return jsonify({"success": False, "error": "暂无调试数据"}), 404
        return jsonify({"success": True, "snapshot": snapshot})

    # ================================================================
    # 角色运行时初始化接口
    # ================================================================

    @app.route("/api/characters/runtime/initialize", methods=["POST"])
    def initialize_character_runtime():
        """初始化角色运行时引擎"""
        try:
            from nbot.character.memory import PromptManagerMemoryAdapter
            from nbot.character.planner import ReactionPlanner
            from nbot.character.policies import SignalAnalyzer
            from nbot.character.repository import (
                CharacterStateRepository,
                ProfileRepository,
                RelationshipRepository,
            )
            from nbot.character.runtime import CharacterRuntime
            from nbot.character.state_machine import StateMachine

            base_dir = _get_base_dir(server)

            from nbot.character.storage.world_book_store import WorldBookStore
            from nbot.character.state_history import StateHistoryStore

            _hook_rt = None
            try:
                from nbot.hooks.manager import get_hook_manager
                _hook_rt = get_hook_manager()
            except Exception:
                pass
            runtime = CharacterRuntime(
                profile_repo=ProfileRepository(base_dir),
                state_repo=CharacterStateRepository(base_dir),
                relationship_repo=RelationshipRepository(base_dir),
                memory_service=PromptManagerMemoryAdapter(),
                signal_analyzer=SignalAnalyzer(),
                planner=ReactionPlanner(),
                state_machine=StateMachine(),
                world_book_store=WorldBookStore(base_dir),
                hook_runtime=_hook_rt,
                history_store=StateHistoryStore(base_dir),
            )

            server.character_runtime = runtime
            _log.info("[CharacterRuntime] 角色运行时引擎已初始化")

            return jsonify({"success": True, "message": "角色运行时引擎已初始化"})
        except Exception as e:
            _log.error("[CharacterRuntime] 初始化失败: %s", e)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/characters/runtime/status", methods=["GET"])
    def get_character_runtime_status():
        """获取角色运行时状态"""
        runtime = _get_character_runtime(server)
        if not runtime:
            return jsonify({"initialized": False})

        return jsonify({
            "initialized": True,
            "has_profile_repo": runtime.profile_repo is not None,
            "has_state_repo": runtime.state_repo is not None,
            "has_relationship_repo": runtime.relationship_repo is not None,
            "has_memory_service": runtime.memory_service is not None,
            "has_signal_analyzer": runtime.signal_analyzer is not None,
            "has_planner": runtime.planner is not None,
            "has_state_machine": runtime.state_machine is not None,
        })
