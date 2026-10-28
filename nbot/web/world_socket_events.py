"""世界模拟的 Socket.IO 事件

事件契约（前端已在用，勿改名）：

- 客户端 → 服务端：``join_world`` / ``leave_world`` / ``world_action`` /
  ``world_tick`` / ``world_jump_time``
- 服务端 → 客户端：``joined_world`` / ``world_state_updated`` /
  ``world_event_created`` / ``world_error``

房间按 ``world:<world_id>` 与 ``world:<world_id>:<node_id>`` 两级划分，
订阅整个世界或单个节点都能收到对应更新。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import request
from flask_socketio import emit, join_room, leave_room

from nbot.world.simulation import WorldSimulation
from nbot.world.store import WorldStore

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def world_room(world_id: str, node_id: str = "") -> str:
    """世界级房间名；传 node_id 时返回节点级房间名。"""
    room = f"world:{world_id}"
    if node_id:
        room = f"{room}:{node_id}"
    return room


def ensure_world_store(server: Any) -> WorldStore:
    """取（或惰性创建）挂在 server 上的 WorldStore。"""
    store = getattr(server, "world_store", None)
    if isinstance(store, WorldStore):
        return store
    # 优先 base_dir（仓库根，世界落 data/worlds）；测试里的假 server 只有 data_dir
    base_dir = getattr(server, "base_dir", None) or getattr(server, "data_dir", ".")
    store = WorldStore(base_dir)
    try:
        server.world_store = store
    except Exception:  # 只读对象时忽略
        pass
    return store


def get_world_model_client(server: Any) -> Optional[Any]:
    """取 server 上的模型调用入口（优先 _get_ai_response）。"""
    if getattr(server, "ai_client", None) is None:
        return None
    responder = getattr(server, "_get_ai_response", None)
    if callable(responder):
        return responder
    caller = getattr(server, "ai_client", None)
    return caller if callable(caller) else None


def _rooms(world_id: str, node_id: str = "") -> list:
    rooms = [world_room(world_id)]
    if node_id:
        rooms.append(world_room(world_id, node_id))
    return rooms


def _to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
    return {}


def _int_or_none(value: Any) -> Optional[int]:
    """socket payload 里的可选非负整数参数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def emit_world_result(server: Any, result: Any, action: str = "") -> None:
    """把一次 step / tick / jump_time 的结果广播给订阅了该世界的客户端。"""
    socketio = getattr(server, "socketio", None)
    if socketio is None:
        return

    payload = _to_dict(result)
    if not payload:
        return
    world_id = str(payload.get("world_id") or "")
    node_id = str(payload.get("node_id") or "")
    payload["action"] = action

    rooms = _rooms(world_id, node_id)
    for room in rooms:
        socketio.emit("world_state_updated", payload, to=room)

    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        message = {
            "world_id": world_id,
            "node_id": event.get("node_id") or node_id,
            "action": action,
            "event": event,
        }
        for room in rooms:
            socketio.emit("world_event_created", message, to=room)


def emit_world_bundle_updated(
    server: Any,
    world_id: str,
    node_id: str = "",
    action: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """世界元数据/节点/角色/资产变更后的轻量通知。"""
    socketio = getattr(server, "socketio", None)
    if socketio is None:
        return

    payload: Dict[str, Any] = {
        "world_id": world_id,
        "node_id": node_id,
        "action": action,
    }
    if extra:
        payload.update(extra)

    for room in _rooms(world_id, node_id):
        socketio.emit("world_state_updated", payload, to=room)


def _emit_error(message: str) -> None:
    emit("world_error", {"message": message}, to=request.sid)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #


def register_world_socket_events(server: Any) -> None:
    """把世界模拟的 socket 事件挂到 server.socketio 上。"""
    socketio = getattr(server, "socketio", None)
    if socketio is None:
        _log.warning("[WorldSocket] server 没有 socketio，跳过世界事件注册")
        return

    store = ensure_world_store(server)

    @socketio.on("join_world")
    def handle_join_world(data=None):
        payload = data if isinstance(data, dict) else {}
        world_id = str(payload.get("world_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        if not world_id:
            _emit_error("world_id is required")
            return

        try:
            bundle = store.get_world_bundle(
                world_id,
                event_limit=_int_or_none(payload.get("event_limit")),
                before_sequence=_int_or_none(payload.get("before_sequence")),
            )
            node = store.get_node(world_id, node_id) if node_id else bundle.get("current_node")
        except KeyError:
            _emit_error("World not found")
            return

        if not node:
            _emit_error("Node not found")
            return

        current_id = str(node.get("node_id") or "")
        join_room(world_room(world_id))
        join_room(world_room(world_id, current_id))
        emit(
            "joined_world",
            {
                "world_id": world_id,
                "node_id": current_id,
                "world": bundle.get("world") or {},
                "state": node.get("state") or {},
                "events": bundle.get("events") or [],
                "event_total": bundle.get("event_total", 0),
                "has_more": bool(bundle.get("has_more")),
            },
        )

    @socketio.on("leave_world")
    def handle_leave_world(data=None):
        payload = data if isinstance(data, dict) else {}
        world_id = str(payload.get("world_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        if not world_id:
            return
        leave_room(world_room(world_id))
        if node_id:
            leave_room(world_room(world_id, node_id))

    @socketio.on("world_action")
    def handle_world_action(data=None):
        payload = data if isinstance(data, dict) else {}
        world_id = str(payload.get("world_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not world_id or not node_id or not content:
            _emit_error("world_id, node_id and content are required")
            return

        try:
            simulation = WorldSimulation(store, model_client=get_world_model_client(server))
            result = simulation.step(
                world_id,
                node_id,
                content,
                target_character_id=str(payload.get("target_character_id") or ""),
                action_type=str(payload.get("action_type") or "act"),
            )
            emit_world_result(server, result, "step")
        except KeyError:
            _emit_error("World or node not found")
        except ValueError as exc:
            _emit_error(str(exc))
        except Exception as exc:
            _log.error("Failed to handle world action: %s", exc)
            _emit_error("World action failed")

    @socketio.on("world_tick")
    def handle_world_tick(data=None):
        payload = data if isinstance(data, dict) else {}
        world_id = str(payload.get("world_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        if not world_id or not node_id:
            _emit_error("world_id and node_id are required")
            return

        try:
            elapsed = int(payload.get("elapsed_seconds", 60))
        except (TypeError, ValueError):
            elapsed = 60

        try:
            simulation = WorldSimulation(store, model_client=get_world_model_client(server))
            result = simulation.tick(world_id, node_id, elapsed_seconds=elapsed)
            emit_world_result(server, result, "tick")
        except KeyError:
            _emit_error("World or node not found")
        except ValueError as exc:
            _emit_error(str(exc))
        except Exception as exc:
            _log.error("Failed to handle world tick: %s", exc)
            _emit_error("World tick failed")

    @socketio.on("world_jump_time")
    def handle_world_jump_time(data=None):
        payload = data if isinstance(data, dict) else {}
        world_id = str(payload.get("world_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        target_time = str(payload.get("target_time") or payload.get("time") or "").strip()
        if not world_id or not node_id or not target_time:
            _emit_error("world_id, node_id and target_time are required")
            return

        try:
            simulation = WorldSimulation(store, model_client=get_world_model_client(server))
            result = simulation.jump_time(world_id, node_id, target_time)
            emit_world_result(server, result, "jump_time")
        except KeyError:
            _emit_error("World or node not found")
        except ValueError as exc:
            _emit_error(str(exc))
        except Exception as exc:
            _log.error("Failed to handle world jump time: %s", exc)
            _emit_error("World jump failed")
