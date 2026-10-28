"""
自定义表情包工具 - 让 AI 能列出、查看与发送自定义表情包

AI 可以调用以下工具：
- list_stickers: 列出表情库中所有自定义表情包
- view_sticker: 查看指定表情包的信息与图片地址
- send_sticker: 向当前会话发送一张自定义表情包图片
"""
import logging
from typing import Any, Dict

from nbot.services.sticker_library import get_sticker, list_stickers
from nbot.services.tool_registry import register_tool

_log = logging.getLogger(__name__)


@register_tool(
    name="list_stickers",
    description="列出表情库中所有可用的自定义表情包名称，用于在回复中引用或发送。",
)
def list_stickers_tool(args: Dict, context: Dict = None) -> Dict[str, Any]:
    try:
        stickers = list_stickers()
        if not stickers:
            return {"success": True, "message": "表情库为空", "stickers": []}
        return {
            "success": True,
            "message": f"共 {len(stickers)} 个表情包",
            "stickers": [{"name": s["name"], "type": s["ext"]} for s in stickers],
        }
    except Exception as e:
        _log.error(f"list_stickers error: {e}")
        return {"success": False, "error": str(e)}


@register_tool(
    name="view_sticker",
    description="查看指定表情包的详细信息与图片地址。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "表情包名称"}
        },
        "required": ["name"],
    },
)
def view_sticker_tool(args: Dict, context: Dict = None) -> Dict[str, Any]:
    try:
        sticker = get_sticker(args.get("name", ""))
        if not sticker:
            return {"success": False, "error": f"表情包 '{args.get('name')}' 不存在"}
        return {
            "success": True,
            "sticker": {
                "name": sticker["name"],
                "url": sticker["url"],
                "type": sticker["ext"],
                "size": sticker["size"],
            },
        }
    except Exception as e:
        _log.error(f"view_sticker error: {e}")
        return {"success": False, "error": str(e)}


@register_tool(
    name="send_sticker",
    description="向当前会话发送一张自定义表情包图片（从表情库按名称发送）。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "表情包名称，需与表情库中的名称一致"}
        },
        "required": ["name"],
    },
)
def send_sticker_tool(args: Dict, context: Dict = None) -> Dict[str, Any]:
    try:
        name = str(args.get("name") or "").strip()
        sticker = get_sticker(name)
        if not sticker:
            available = [s["name"] for s in list_stickers()[:20]]
            return {
                "success": False,
                "error": f"表情包 '{name}' 不存在",
                "available": available,
            }
        # _send_image 由管道 on_tool_result 钩子处理，触发真实的图片发送
        return {
            "success": True,
            "message": f"已发送表情包: {sticker['name']}",
            "_send_image": {
                "url": sticker["url"],
                "prompt": sticker["name"],
                "trigger": "sticker_tool",
            },
        }
    except Exception as e:
        _log.error(f"send_sticker error: {e}")
        return {"success": False, "error": str(e)}
