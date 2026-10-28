"""插件 manifest：解析、校验与设置项归一化。

manifest 位于 ``data/plugins/<id>/plugin.json``，字段见 ``nbot/plugins/DESIGN.md``。
所有校验都是 fail-closed：字段缺失、类型不符、未知权限一律拒绝加载。
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nbot.plugins import safety
from nbot.plugins.permissions import PermissionFormatError, parse_permission

#: 当前宿主支持的插件 API 版本
API_VERSION = "1"
#: manifest 文件名
MANIFEST_FILENAME = "plugin.json"
#: manifest 文件大小上限（64KB）
MAX_MANIFEST_BYTES = 64 * 1024

#: 插件 id：小写字母/数字开头，只含小写字母、数字、下划线、短横线，长度 2-32
PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
#: 语义化版本号（允许预发布/构建后缀）
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
#: 插件动作名
ACTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: 设置项键名
SETTING_KEY_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")

SETTING_TYPES = frozenset({"string", "text", "number", "boolean", "select"})


class ManifestError(ValueError):
    """manifest 缺失、格式错误或字段非法。"""


@dataclass(frozen=True)
class PluginManifest:
    """校验通过的插件 manifest。"""

    id: str
    name: str
    version: str
    api_version: str = API_VERSION
    description: str = ""
    author: str = ""
    homepage: str = ""
    entry: str = "main.py"
    ui: Dict[str, Any] = field(default_factory=dict)
    permissions: Tuple[str, ...] = ()
    settings_schema: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    actions: Tuple[Dict[str, Any], ...] = ()
    plugin_dir: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.plugin_dir, MANIFEST_FILENAME) if self.plugin_dir else ""

    @property
    def entry_path(self) -> str:
        """入口文件的绝对路径（已校验落在插件目录内）。"""
        if not self.plugin_dir:
            return ""
        return safety.resolve_inside(self.plugin_dir, self.entry)

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


def _require_text(raw: Dict[str, Any], key: str, *, limit: int, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ManifestError(f"字段 {key} 必须是字符串")
    text = value.strip()
    if len(text) > limit:
        raise ManifestError(f"字段 {key} 超过 {limit} 字符")
    return text


def _parse_ui(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw_ui = raw.get("ui")
    if raw_ui is None:
        return {}
    if not isinstance(raw_ui, dict):
        raise ManifestError("字段 ui 必须是对象")
    ui: Dict[str, Any] = {}
    page = raw_ui.get("page")
    if page is not None:
        if not isinstance(page, str) or not page.strip():
            raise ManifestError("ui.page 必须是非空字符串")
        normalized = safety.normalize_relative(page)
        if not normalized.lower().endswith((".html", ".htm")):
            raise ManifestError("ui.page 必须是 .html 文件")
        ui["page"] = normalized
    title = raw_ui.get("title")
    if title is not None:
        if not isinstance(title, str) or len(title.strip()) > 64:
            raise ManifestError("ui.title 必须是不超过 64 字符的字符串")
        ui["title"] = title.strip()
    icon = raw_ui.get("icon")
    if icon is not None:
        if not isinstance(icon, str) or len(icon.strip()) > 64:
            raise ManifestError("ui.icon 必须是不超过 64 字符的字符串")
        ui["icon"] = icon.strip()
    return ui


def _parse_permissions(raw: Dict[str, Any]) -> Tuple[str, ...]:
    raw_permissions = raw.get("permissions", [])
    if raw_permissions is None:
        raw_permissions = []
    if not isinstance(raw_permissions, (list, tuple)):
        raise ManifestError("字段 permissions 必须是字符串数组")
    permissions = []
    for item in raw_permissions:
        if not isinstance(item, str):
            raise ManifestError("permissions 只能包含字符串")
        try:
            parsed = parse_permission(item)
        except PermissionFormatError as exc:
            raise ManifestError(f"未知权限 {item!r}: {exc}") from exc
        text = parsed.text
        if text not in permissions:
            permissions.append(text)
    return tuple(permissions)


def _parse_settings_schema(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw_schema = raw.get("settings_schema")
    if raw_schema is None:
        return {}
    if not isinstance(raw_schema, dict):
        raise ManifestError("字段 settings_schema 必须是对象")
    schema: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_spec in raw_schema.items():
        key = str(raw_key or "").strip()
        if not SETTING_KEY_PATTERN.match(key):
            raise ManifestError(f"设置项键名非法: {raw_key!r}")
        if not isinstance(raw_spec, dict):
            raise ManifestError(f"设置项 {key} 的定义必须是对象")
        setting_type = str(raw_spec.get("type") or "string").strip().lower()
        if setting_type not in SETTING_TYPES:
            raise ManifestError(f"设置项 {key} 的类型不支持: {setting_type}")
        spec: Dict[str, Any] = {"type": setting_type}
        options_values: List[Any] = []
        label = raw_spec.get("label")
        if label is not None:
            if not isinstance(label, str) or len(label) > 64:
                raise ManifestError(f"设置项 {key} 的 label 非法")
            spec["label"] = label.strip()
        if setting_type == "select":
            options = raw_spec.get("options")
            if not isinstance(options, list) or not options:
                raise ManifestError(f"设置项 {key} 是 select，必须提供 options")
            normalized_options = []
            for option in options:
                if isinstance(option, dict):
                    value = option.get("value")
                    text = str(option.get("label") or value or "").strip()
                else:
                    value = option
                    text = str(option).strip()
                if value is None or text == "":
                    raise ManifestError(f"设置项 {key} 的 options 项非法")
                normalized_options.append({"value": value, "label": text})
            spec["options"] = normalized_options
            options_values = [option["value"] for option in normalized_options]
            if "default" in raw_spec and raw_spec["default"] not in options_values:
                raise ManifestError(f"设置项 {key} 的默认值不在 options 中")
        if "default" in raw_spec:
            spec["default"] = _coerce_setting(
                key, setting_type, raw_spec["default"], options=options_values
            )
        else:
            spec["default"] = _default_for_type(setting_type, spec)
        schema[key] = spec
    return schema


def _default_for_type(setting_type: str, spec: Dict[str, Any]) -> Any:
    if setting_type == "number":
        return 0
    if setting_type == "boolean":
        return False
    if setting_type == "select":
        options = spec.get("options") or []
        return options[0]["value"] if options else ""
    return ""


def _coerce_setting(
    key: str,
    setting_type: str,
    value: Any,
    *,
    options: Optional[Iterable[Any]] = None,
) -> Any:
    """把单个设置值转换为声明类型，失败抛 :class:`ManifestError`。"""
    if setting_type in ("string", "text"):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ManifestError(f"设置项 {key} 需要字符串")
        return str(value)
    if setting_type == "number":
        if isinstance(value, bool):
            raise ManifestError(f"设置项 {key} 需要数字")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value) if "." in value else int(value)
            except ValueError as exc:
                raise ManifestError(f"设置项 {key} 需要数字") from exc
        raise ManifestError(f"设置项 {key} 需要数字")
    if setting_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
            return False
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ManifestError(f"设置项 {key} 需要布尔值")
    if setting_type == "select":
        allowed = list(options or [])
        if allowed and value not in allowed:
            raise ManifestError(f"设置项 {key} 的取值不在 options 中")
        return value
    raise ManifestError(f"未知设置类型: {setting_type}")


def _parse_actions(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    raw_actions = raw.get("actions")
    if raw_actions is None:
        return ()
    if not isinstance(raw_actions, list):
        raise ManifestError("字段 actions 必须是数组")
    actions = []
    seen = set()
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            raise ManifestError("actions 的每一项必须是对象")
        name = str(raw_action.get("name") or "").strip()
        if not ACTION_NAME_PATTERN.match(name):
            raise ManifestError(f"动作名非法: {name!r}")
        if name in seen:
            raise ManifestError(f"动作名重复: {name}")
        seen.add(name)
        description = raw_action.get("description")
        if description is not None and not isinstance(description, str):
            raise ManifestError(f"动作 {name} 的 description 必须是字符串")
        parameters = raw_action.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise ManifestError(f"动作 {name} 的 parameters 必须是 JSON Schema 对象")
        actions.append(
            {
                "name": name,
                "description": str(description or "")[:200],
                "parameters": parameters
                or {"type": "object", "properties": {}},
            }
        )
    return tuple(actions)


def parse_manifest(raw: Any, *, plugin_dir: str = "") -> PluginManifest:
    """校验并构造 :class:`PluginManifest`。"""
    if not isinstance(raw, dict):
        raise ManifestError("plugin.json 必须是 JSON 对象")

    plugin_id = _require_text(raw, "id", limit=32)
    if not PLUGIN_ID_PATTERN.match(plugin_id):
        raise ManifestError(f"插件 id 非法: {plugin_id!r}（小写字母/数字开头，2-32 位）")
    name = _require_text(raw, "name", limit=64)
    if not name:
        raise ManifestError("plugin.json 缺少 name")
    version = _require_text(raw, "version", limit=32)
    if not VERSION_PATTERN.match(version):
        raise ManifestError(f"版本号必须是语义化版本: {version!r}")

    api_version = _require_text(raw, "api_version", limit=16, default=API_VERSION)
    if api_version != API_VERSION:
        raise ManifestError(
            f"插件 api_version={api_version} 与宿主 {API_VERSION} 不匹配，拒绝加载"
        )

    entry = _require_text(raw, "entry", limit=120, default="main.py")
    if not entry:
        entry = "main.py"
    if not entry.endswith(".py"):
        raise ManifestError("entry 必须是 .py 文件")
    try:
        entry = safety.normalize_relative(entry)
    except safety.UnsafePathError as exc:
        raise ManifestError(f"entry 路径非法: {exc}") from exc

    ui = _parse_ui(raw)
    permissions = _parse_permissions(raw)
    if ui and "ui.page" not in permissions:
        raise ManifestError("声明了 ui 页面但缺少 ui.page 权限")

    if plugin_dir:
        # 入口必须真实存在于插件目录内（安装/启用时校验，解析阶段也做一次路径安全检查）
        safety.resolve_inside(plugin_dir, entry)

    actions = _parse_actions(raw)
    if actions and "tool.register" not in permissions:
        raise ManifestError("声明了 actions 但缺少 tool.register 权限")

    schema = _parse_settings_schema(raw)

    return PluginManifest(
        id=plugin_id,
        name=name,
        version=version,
        api_version=api_version,
        description=_require_text(raw, "description", limit=2000),
        author=_require_text(raw, "author", limit=64),
        homepage=_require_text(raw, "homepage", limit=300),
        entry=entry,
        ui=ui,
        permissions=permissions,
        settings_schema=schema,
        actions=actions,
        plugin_dir=plugin_dir,
        raw=dict(raw),
    )


def load_manifest(plugin_dir: str) -> PluginManifest:
    """从插件目录读取并校验 manifest。"""
    manifest_path = os.path.join(plugin_dir, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        raise ManifestError(f"缺少 {MANIFEST_FILENAME}")
    size = os.path.getsize(manifest_path)
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"{MANIFEST_FILENAME} 超过 {MAX_MANIFEST_BYTES // 1024}KB 上限")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{MANIFEST_FILENAME} 不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"读取 {MANIFEST_FILENAME} 失败: {exc}") from exc
    return parse_manifest(raw, plugin_dir=plugin_dir)


def normalize_settings(manifest: PluginManifest, values: Any) -> Dict[str, Any]:
    """按 manifest.settings_schema 归一化设置值：补默认值、丢弃未声明键、校验类型。"""
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ManifestError("settings 必须是对象")

    unknown = [str(key) for key in values if str(key) not in manifest.settings_schema]
    if unknown:
        raise ManifestError(f"未声明的设置项: {', '.join(sorted(unknown)[:5])}")

    result: Dict[str, Any] = {}
    for key, spec in manifest.settings_schema.items():
        setting_type = str(spec.get("type") or "string")
        options = [option.get("value") for option in spec.get("options") or []]
        if key in values:
            result[key] = _coerce_setting(
                key, setting_type, values[key], options=options
            )
        else:
            result[key] = spec.get("default")
    return result


def manifest_summary(
    manifest: PluginManifest, *, effective_permissions: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """给 Web 面板用的 manifest 摘要。"""
    permissions = list(effective_permissions if effective_permissions is not None else [])
    return {
        "id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "api_version": manifest.api_version,
        "description": manifest.description,
        "author": manifest.author,
        "homepage": manifest.homepage,
        "entry": manifest.entry,
        "ui": dict(manifest.ui),
        "declared_permissions": list(manifest.permissions),
        "effective_permissions": permissions,
        "settings_schema": dict(manifest.settings_schema),
        "actions": [dict(action) for action in manifest.actions],
    }
