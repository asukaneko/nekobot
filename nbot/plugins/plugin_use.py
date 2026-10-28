"""``plugin_use`` 元工具：列出 / 描述 / 调用已启用插件，并提供页面入口。

注册走 ``nbot.services.tools.register_external_tool``（source=``plugin:core``），
描述按当前已启用插件动态生成；一个插件都没有时直接注销该工具，避免 prompt 膨胀。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from nbot.plugins.runtime import PluginNotFound, PluginRuntime, get_plugin_runtime

_log = logging.getLogger(__name__)

TOOL_NAME = "plugin_use"
SOURCE = "plugin:core"
MAX_PLUGINS_IN_DESCRIPTION = 20
MAX_DESCRIPTION_CHARS = 1500
MAX_LIST_CHARS = 4000

ACTION_DESCRIPTIONS = (
    "list：列出已启用插件；"
    "describe：查看插件详情（描述、动作、权限）；"
    "call：调用插件动作，params 传 {action: 动作名, arguments: {...}}；"
    "ui：取插件页面地址（Web 面板内展示）"
)


def _tool_api() -> Any:
    from nbot.services import tools as tool_api

    return tool_api


def _summaries(runtime: PluginRuntime) -> List[Dict[str, Any]]:
    try:
        return runtime.enabled_plugin_summaries()
    except Exception as exc:  # 插件异常不能拖垮工具注册
        _log.warning("[plugin_use] 读取插件列表失败: %s", exc)
        return []


def _actions_of(summary: Dict[str, Any]) -> List[str]:
    names = list(summary.get("provided_actions") or [])
    for action in summary.get("actions") or []:
        name = str(action.get("name") or "")
        if name and name not in names:
            names.append(name)
    return names


def build_spec(runtime: PluginRuntime) -> Dict[str, Any]:
    """生成工具 spec：description 只列已启用插件。"""
    summaries = _summaries(runtime)[:MAX_PLUGINS_IN_DESCRIPTION]
    lines = []
    for summary in summaries:
        actions = _actions_of(summary)
        action_text = f"（动作: {', '.join(actions)}）" if actions else ""
        description = str(summary.get("description") or "").replace("\n", " ")[:120]
        lines.append(f"- {summary['id']}: {summary['name']} — {description}{action_text}")

    header = "调用 NekoBot 插件。已启用插件：\n"
    description = header + "\n".join(lines) + f"\n动作说明：{ACTION_DESCRIPTIONS}"
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 3] + "..."

    plugin_ids = [str(summary["id"]) for summary in summaries]
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "describe", "call", "ui"],
                    "description": ACTION_DESCRIPTIONS,
                },
                "plugin_id": {
                    "type": "string",
                    "enum": plugin_ids,
                    "description": "插件 id（describe / call / ui 必填）",
                },
                "params": {
                    "type": "object",
                    "description": "call 时传 {action: 动作名, arguments: {...}}",
                },
            },
            "required": ["action"],
        },
    }


def _text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def handle_plugin_use(args: Any, context: Any = None) -> Dict[str, Any]:
    """plugin_use 工具入口；``context`` 为宿主工具上下文（可忽略）。"""
    runtime = get_plugin_runtime()
    payload = args if isinstance(args, dict) else {}
    action = str(payload.get("action") or "list").strip().lower()
    plugin_id = str(payload.get("plugin_id") or "").strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return {"success": False, "content": "params 必须是对象"}

    if action == "list":
        summaries = _summaries(runtime)
        if not summaries:
            return {"success": True, "content": "当前没有已启用的插件。"}
        lines = [
            f"- {item['id']}（{item['name']} v{item['version']}）: "
            f"{str(item.get('description') or '无描述')[:120]}"
            for item in summaries
        ]
        return {"success": True, "content": ("已启用插件：\n" + "\n".join(lines))[:MAX_LIST_CHARS]}

    if not plugin_id:
        return {"success": False, "content": "plugin_id 必填"}

    try:
        detail = runtime.get_plugin(plugin_id)
    except PluginNotFound as exc:
        return {"success": False, "content": str(exc)}

    if action == "describe":
        actions = _actions_of(detail)
        permissions = [item["name"] for item in detail.get("permissions") or []]
        info = {
            "id": detail["id"],
            "name": detail["name"],
            "version": detail["version"],
            "description": detail.get("description") or "",
            "enabled": detail.get("enabled"),
            "actions": actions,
            "permissions": permissions,
            "settings_schema": detail.get("settings_schema") or {},
            "last_error": detail.get("last_error"),
        }
        return {"success": True, "content": _text(info)}

    if action == "ui":
        if not detail.get("loaded"):
            return {"success": False, "content": f"插件 {plugin_id} 未启用"}
        url = detail.get("ui_url")
        if not url:
            return {"success": False, "content": f"插件 {plugin_id} 没有提供页面"}
        return {"success": True, "content": url}

    if action == "call":
        action_name = str(params.get("action") or "").strip()
        if not action_name:
            return {"success": False, "content": "call 需要在 params.action 指定动作名"}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"success": False, "content": "params.arguments 必须是对象"}
        try:
            outcome = runtime.invoke_action(plugin_id, action_name, arguments)
        except Exception as exc:  # 插件异常转成工具错误，不打断工具循环
            _log.warning("[plugin_use] 调用 %s.%s 失败: %s", plugin_id, action_name, exc)
            return {
                "success": False,
                "error": str(exc),
                "content": f"插件动作调用失败: {exc}",
            }
        return {"success": True, "content": _text(outcome)}

    return {"success": False, "content": f"未知 action: {action}"}


def refresh_plugin_use_tool(runtime: Optional[PluginRuntime] = None) -> bool:
    """按当前启用插件注册/注销 ``plugin_use``；返回是否处于注册状态。"""
    active = runtime if runtime is not None else get_plugin_runtime()
    api = _tool_api()
    summaries = _summaries(active)
    if not summaries:
        api.unregister_external_tool(TOOL_NAME, source=SOURCE)
        return False
    return bool(
        api.register_external_tool(
            TOOL_NAME,
            build_spec(active),
            handle_plugin_use,
            source=SOURCE,
            read_only=False,
        )
    )
