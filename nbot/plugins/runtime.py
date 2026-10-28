"""插件运行时：安装、启用、停用、卸载、热重载与状态持久化。

目录约定::

    data/plugins/<id>/plugin.json     # manifest
    data/plugins/<id>/main.py         # 入口（导出 register(ctx) / unregister(ctx)）
    data/web/plugins.json             # 启用状态、设置值、权限收紧策略

安全约束（均在本模块或 :mod:`nbot.plugins.safety` 里落实）：

- ZIP 解压前预检文件数、体积、后缀、``..``、盘符与软链；
- 未知/非法权限 fail-closed，manifest 校验失败不落状态；
- 所有文件访问走 ``realpath`` 校验，必须落在插件目录内。
"""

import importlib.util
import inspect
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Any, Dict, Iterable, List, Optional

from nbot.plugins import safety
from nbot.plugins.host import (
    ACTION_TIMEOUT_SECONDS,
    HostServices,
    PluginContext,
    PluginHost,
    run_with_timeout,
)
from nbot.plugins.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    PluginManifest,
    load_manifest,
    manifest_summary,
    normalize_settings,
)
from nbot.plugins.permissions import (
    PermissionGuard,
    effective_permissions,
    permissions_for_display,
)
from nbot.plugins.registry import PluginRegistry

_log = logging.getLogger(__name__)

STATE_FILENAME = "plugins.json"
STATE_VERSION = 1
MODULE_PREFIX = "nbot_plugin_"


class PluginError(RuntimeError):
    """插件子系统通用错误。"""


class PluginNotFound(PluginError):
    """插件不存在。"""


class PluginInstallError(PluginError):
    """安装失败（ZIP 非法、已存在、落盘失败）。"""


class PluginLoadError(PluginError):
    """启用/重载失败（入口缺失、register 抛错）。"""


class PluginStateError(PluginError):
    """状态文件读写失败。"""


def repository_root() -> str:
    """仓库根目录（nbot/plugins/runtime.py 向上三级）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_plugins_root() -> str:
    override = os.getenv("NBOT_PLUGINS_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(repository_root(), "data", "plugins")


def default_state_path() -> str:
    override = os.getenv("NBOT_PLUGIN_STATE", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(repository_root(), "data", "web", STATE_FILENAME)


def _sanitize_settings(
    manifest: PluginManifest, values: Any
) -> Dict[str, Any]:
    """安装/覆盖时清洗旧设置：丢弃未声明或类型不符的键，保留合法值。"""
    if not isinstance(values, dict):
        return {}
    candidate = {key: value for key, value in values.items() if key in manifest.settings_schema}
    if not candidate:
        return {}
    try:
        return normalize_settings(manifest, candidate)
    except ManifestError:
        cleaned: Dict[str, Any] = {}
        for key, value in candidate.items():
            try:
                cleaned.update(normalize_settings(manifest, {key: value}))
            except ManifestError:
                _log.debug("[Plugins] 丢弃非法设置项 %s.%s", manifest.id, key)
        return cleaned


def _sanitize_override(
    manifest: PluginManifest, override: Any
) -> Optional[List[str]]:
    """清洗旧权限策略：只保留仍被新 manifest 声明的权限。"""
    if not isinstance(override, (list, tuple)):
        return None
    declared = set(manifest.permissions)
    return [str(item) for item in override if str(item) in declared]


class PluginRuntime:
    """插件运行时（单例由 :func:`get_plugin_runtime` 提供）。"""

    def __init__(
        self,
        plugins_root: Optional[str] = None,
        state_path: Optional[str] = None,
        registry: Optional[PluginRegistry] = None,
        services: Optional[HostServices] = None,
    ):
        self._root = os.path.abspath(plugins_root or default_plugins_root())
        self._state_path = os.path.abspath(state_path or default_state_path())
        self._registry = registry if registry is not None else PluginRegistry()
        self._services = services if services is not None else HostServices()
        self._lock = threading.RLock()
        self._manifests: Dict[str, PluginManifest] = {}
        self._modules: Dict[str, Any] = {}
        self._contexts: Dict[str, PluginContext] = {}
        self._hosts: Dict[str, PluginHost] = {}
        self._errors: List[Dict[str, str]] = []
        self._state: Dict[str, Any] = {"version": STATE_VERSION, "plugins": {}}
        self._state_loaded = False
        self._scanned = False
        self._bootstrapped = False

    # -- 基础属性 ---------------------------------------------------------

    @property
    def plugins_root(self) -> str:
        return self._root

    @property
    def state_path(self) -> str:
        return self._state_path

    @property
    def registry(self) -> PluginRegistry:
        return self._registry

    def host_for(self, plugin_id: str) -> Optional[PluginHost]:
        """已加载插件的宿主实例（未启用返回 None）。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            return self._hosts.get(pid)

    # -- 状态文件 ---------------------------------------------------------

    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        self._state = {"version": STATE_VERSION, "plugins": {}}
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("[Plugins] 读取状态文件失败 (%s): %s", self._state_path, exc)
            return
        if not isinstance(data, dict):
            _log.warning("[Plugins] 状态文件不是对象，已重置: %s", self._state_path)
            return
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
        self._state = {
            "version": data.get("version") or STATE_VERSION,
            "plugins": {
                str(pid): dict(entry)
                for pid, entry in plugins.items()
                if isinstance(entry, dict)
            },
        }

    def _save_state(self) -> None:
        directory = os.path.dirname(self._state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self._state_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._state_path)
        except OSError as exc:
            raise PluginStateError(f"写入插件状态失败: {exc}") from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _state_view(self, plugin_id: str) -> Dict[str, Any]:
        self._load_state()
        entry = (self._state.get("plugins") or {}).get(plugin_id) or {}
        return {
            "enabled": bool(entry.get("enabled")),
            "settings": dict(entry.get("settings") or {}),
            "permissions_override": entry.get("permissions_override"),
            "installed_at": entry.get("installed_at"),
            "updated_at": entry.get("updated_at"),
            "enabled_at": entry.get("enabled_at"),
            "version": entry.get("version"),
            "last_error": entry.get("last_error"),
        }

    def _write_state(self, plugin_id: str, **fields: Any) -> None:
        self._load_state()
        plugins = self._state.setdefault("plugins", {})
        entry = plugins.get(plugin_id)
        if not isinstance(entry, dict):
            entry = {}
            plugins[plugin_id] = entry
        entry.update(fields)
        entry.setdefault("settings", {})
        self._save_state()

    # -- 扫描与查询 -------------------------------------------------------

    def _ensure_scan(self, *, refresh: bool = False) -> None:
        if self._scanned and not refresh:
            return
        self._scanned = True
        self._manifests = {}
        self._errors = []
        if not os.path.isdir(self._root):
            return
        for name in sorted(os.listdir(self._root)):
            if name.startswith("."):
                continue
            path = os.path.join(self._root, name)
            if not os.path.isdir(path):
                continue
            try:
                manifest = load_manifest(path)
            except ManifestError as exc:
                self._errors.append({"id": name, "path": path, "error": str(exc)})
                continue
            if manifest.id != name:
                self._errors.append(
                    {
                        "id": name,
                        "path": path,
                        "error": f"目录名与 manifest id ({manifest.id}) 不一致",
                    }
                )
                continue
            self._manifests[manifest.id] = manifest

    def _manifest(self, plugin_id: str) -> PluginManifest:
        self._ensure_scan()
        manifest = self._manifests.get(plugin_id)
        if manifest is None:
            raise PluginNotFound(f"插件不存在: {plugin_id}")
        return manifest

    @staticmethod
    def _require_id(plugin_id: str) -> str:
        pid = str(plugin_id or "").strip()
        if not pid:
            raise PluginNotFound("插件 id 不能为空")
        return pid

    def _module_name(self, plugin_id: str) -> str:
        return MODULE_PREFIX + plugin_id.replace("-", "_")

    def _detail(self, plugin_id: str) -> Dict[str, Any]:
        manifest = self._manifests[plugin_id]
        state = self._state_view(plugin_id)
        guard = PermissionGuard(
            plugin_id, manifest.permissions, state["permissions_override"]
        )
        data = manifest_summary(manifest, effective_permissions=guard.effective)
        try:
            settings = normalize_settings(manifest, state["settings"])
        except ManifestError as exc:
            settings = dict(state["settings"])
            data["settings_error"] = str(exc)
        data.update(
            {
                "enabled": state["enabled"],
                "loaded": plugin_id in self._modules,
                "settings": settings,
                "settings_values": dict(state["settings"]),
                "permissions": permissions_for_display(guard.effective),
                "declared_permissions_display": permissions_for_display(
                    manifest.permissions
                ),
                "last_error": state["last_error"],
                "installed_at": state["installed_at"],
                "updated_at": state["updated_at"],
                "enabled_at": state["enabled_at"],
                "path": manifest.plugin_dir,
                "resources": self._registry.plugin_summary(plugin_id),
            }
        )
        page = (manifest.ui or {}).get("page")
        if page:
            data["ui_url"] = f"/api/plugins/{plugin_id}/ui/{page}"
        ctx = self._contexts.get(plugin_id)
        data["provided_actions"] = sorted(ctx.actions) if ctx is not None else []
        return data

    def list_plugins(self) -> Dict[str, Any]:
        """列出全部插件（含 manifest 非法的目录错误）。"""
        with self._lock:
            self.ensure_bootstrapped()
            self._ensure_scan()
            return {
                "plugins": [self._detail(pid) for pid in sorted(self._manifests)],
                "errors": list(self._errors),
                "root": self._root,
            }

    def get_plugin(self, plugin_id: str) -> Dict[str, Any]:
        pid = self._require_id(plugin_id)
        with self._lock:
            self.ensure_bootstrapped()
            self._manifest(pid)
            return self._detail(pid)

    def ensure_bootstrapped(self) -> int:
        """按状态文件启用已启用的插件（首次调用时执行，返回启用数量）。"""
        with self._lock:
            if self._bootstrapped:
                return 0
            self._bootstrapped = True
            self._load_state()
            self._ensure_scan()
            enabled_ids = [
                pid
                for pid, entry in (self._state.get("plugins") or {}).items()
                if isinstance(entry, dict) and entry.get("enabled")
            ]
        enabled = 0
        for pid in sorted(enabled_ids):
            try:
                self.enable(pid)
                enabled += 1
            except PluginError as exc:
                _log.warning("[Plugins] 启动启用 %s 失败: %s", pid, exc)
        self._refresh_core_tool()
        return enabled

    # -- 安装 -------------------------------------------------------------

    def install_from_zip(self, blob: bytes, *, overwrite: bool = False) -> Dict[str, Any]:
        """从 ZIP 字节流安装插件，返回插件详情。"""
        if not blob:
            raise PluginInstallError("插件包为空")
        if len(blob) > safety.MAX_PLUGIN_ZIP_BYTES:
            limit_mb = safety.MAX_PLUGIN_ZIP_BYTES // (1024 * 1024)
            raise PluginInstallError(f"插件包超过 {limit_mb}MB 上限")

        staging = tempfile.mkdtemp(prefix="nbot-plugin-")
        try:
            try:
                archive = zipfile.ZipFile(io.BytesIO(blob))
            except (zipfile.BadZipFile, EOFError, ValueError, OSError) as exc:
                raise PluginInstallError("ZIP 文件损坏或格式不正确") from exc
            staging_root = os.path.join(staging, "package")
            with archive:
                try:
                    safety.extract_zip(archive, staging_root)
                except safety.ZipLimitError as exc:
                    raise PluginInstallError(f"插件包被拒绝: {exc}") from exc
                except safety.UnsafePathError as exc:
                    raise PluginInstallError(f"插件包路径不安全: {exc}") from exc

            plugin_dir = self._locate_plugin_dir(staging_root)
            try:
                manifest = load_manifest(plugin_dir)
            except ManifestError as exc:
                raise PluginInstallError(f"manifest 校验失败: {exc}") from exc

            with self._lock:
                return self._install_manifest_dir(manifest, plugin_dir, overwrite=overwrite)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _locate_plugin_dir(root: str) -> str:
        if os.path.isfile(os.path.join(root, MANIFEST_FILENAME)):
            return root
        sub_dirs = [
            name
            for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
            and os.path.isfile(os.path.join(root, name, MANIFEST_FILENAME))
        ]
        if len(sub_dirs) == 1:
            return os.path.join(root, sub_dirs[0])
        raise PluginInstallError(
            f"插件包内未找到唯一的 {MANIFEST_FILENAME}（发现 {len(sub_dirs)} 个候选目录）"
        )

    def _install_manifest_dir(
        self, manifest: PluginManifest, source_dir: str, *, overwrite: bool
    ) -> Dict[str, Any]:
        pid = manifest.id
        self._ensure_scan()
        try:
            target = safety.resolve_inside(self._root, pid)
        except safety.UnsafePathError as exc:
            raise PluginInstallError(f"插件 id 非法: {pid}") from exc

        exists = os.path.isdir(target) or pid in self._manifests
        if exists and not overwrite:
            raise PluginInstallError(f"插件 {pid} 已安装，覆盖安装需显式指定 overwrite")

        previous = self._state_view(pid)
        was_enabled = previous["enabled"] or pid in self._modules
        if pid in self._modules:
            self._deactivate(pid)

        os.makedirs(self._root, exist_ok=True)
        backup: Optional[str] = None
        if os.path.isdir(target):
            backup = os.path.join(self._root, f".{pid}.bak-{uuid.uuid4().hex[:8]}")
            os.replace(target, backup)
        try:
            shutil.move(source_dir, target)
        except OSError as exc:
            if backup and not os.path.isdir(target):
                os.replace(backup, target)
                backup = None
            raise PluginInstallError(f"写入插件目录失败: {exc}") from exc
        finally:
            if backup:
                shutil.rmtree(backup, ignore_errors=True)

        try:
            installed_manifest = load_manifest(target)
        except ManifestError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise PluginInstallError(f"安装后校验失败: {exc}") from exc

        now = time.time()
        self._write_state(
            pid,
            enabled=False,
            version=installed_manifest.version,
            installed_at=previous["installed_at"] or now,
            updated_at=now,
            enabled_at=None,
            last_error=None,
            settings=_sanitize_settings(installed_manifest, previous["settings"]),
            permissions_override=_sanitize_override(
                installed_manifest, previous["permissions_override"]
            ),
        )
        self._manifests[pid] = installed_manifest
        self._scanned = True

        if was_enabled:
            try:
                self.enable(pid)
            except PluginError as exc:
                _log.warning("[Plugins] 覆盖安装后重新启用 %s 失败: %s", pid, exc)
        self._refresh_core_tool()
        return self._detail(pid)

    # -- 生命周期 ---------------------------------------------------------

    def enable(self, plugin_id: str) -> Dict[str, Any]:
        """启用插件：加载入口并调用 ``register(ctx)``。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            self.ensure_bootstrapped()
            if pid in self._modules:
                return self._detail(pid)
            manifest = self._manifest(pid)
            state = self._state_view(pid)
            guard = PermissionGuard(pid, manifest.permissions, state["permissions_override"])
            try:
                settings = normalize_settings(manifest, state["settings"])
            except ManifestError as exc:
                raise PluginLoadError(f"插件 {pid} 的设置值非法: {exc}") from exc

            error = self._activate(manifest, guard, settings)
            if error:
                raise PluginLoadError(f"插件 {pid} 启用失败: {error}")
            self._write_state(
                pid,
                enabled=True,
                version=manifest.version,
                enabled_at=time.time(),
                last_error=None,
            )
            self._refresh_core_tool()
            return self._detail(pid)

    def _activate(
        self,
        manifest: PluginManifest,
        guard: PermissionGuard,
        settings: Dict[str, Any],
    ) -> Optional[str]:
        pid = manifest.id
        plugin_log = logging.getLogger(f"nbot.plugin.{pid}")
        host = PluginHost(
            plugin_id=pid,
            manifest=manifest,
            settings=dict(settings),
            guard=guard,
            registry=self._registry,
            plugin_dir=manifest.plugin_dir,
            data_dir=os.path.join(self._root, ".data"),
            services=self._services,
            logger=plugin_log,
        )
        ctx = PluginContext(
            plugin_id=pid,
            manifest=manifest,
            settings=dict(settings),
            guard=guard,
            registry=self._registry,
            log=plugin_log,
            api=host.api,
            host=host,
        )
        self._hosts[pid] = host
        try:
            module = self._load_module(manifest)
            self._modules[pid] = module
            self._contexts[pid] = ctx
            register = getattr(module, "register", None)
            if register is None or not callable(register):
                raise PluginLoadError("入口未导出 register(ctx)")
            outcome = register(ctx)
            if inspect.isawaitable(outcome):
                raise PluginLoadError("register 不支持协程，请使用同步函数")
            self._collect_actions(pid, module, ctx)
        except Exception as exc:  # 插件代码异常不能污染宿主
            self._deactivate(pid)
            self._write_state(pid, enabled=False, last_error=str(exc))
            _log.warning("[Plugins] 启用 %s 失败: %s", pid, exc)
            return str(exc)
        return None

    def _collect_actions(self, plugin_id: str, module: Any, ctx: PluginContext) -> None:
        """收集插件动作：``ctx.register_action()`` 或入口模块的 ``actions``。"""
        declared = {action["name"] for action in ctx.manifest.actions}
        provided: Dict[str, Any] = {}
        raw_actions = getattr(module, "actions", None)
        if callable(raw_actions):
            try:
                outcome = raw_actions(ctx)
            except Exception as exc:
                _log.warning("[Plugins] %s 的 actions(ctx) 失败: %s", plugin_id, exc)
                outcome = None
            if isinstance(outcome, dict):
                provided.update(outcome)
        elif isinstance(raw_actions, dict):
            provided.update(raw_actions)

        merged = dict(provided)
        merged.update(ctx.actions)
        for name, handler in merged.items():
            action_name = str(name)
            if declared and action_name not in declared:
                _log.warning(
                    "[Plugins] 动作 %s.%s 未在 manifest 声明，已忽略", plugin_id, action_name
                )
                continue
            if not callable(handler):
                _log.warning("[Plugins] 动作 %s.%s 不可调用，已忽略", plugin_id, action_name)
                continue
            ctx.actions[action_name] = handler
        _log.debug("[Plugins] %s 动作: %s", plugin_id, sorted(ctx.actions))

    def invoke_action(
        self,
        plugin_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = ACTION_TIMEOUT_SECONDS,
    ) -> Any:
        """调用插件动作（线程执行 + 超时保护）。"""
        pid = self._require_id(plugin_id)
        name = str(action_name or "").strip()
        with self._lock:
            if pid not in self._modules:
                raise PluginLoadError(f"插件 {pid} 未启用")
            ctx = self._contexts.get(pid)
            handler = ctx.actions.get(name) if ctx is not None else None
        if handler is None:
            raise PluginError(f"插件 {pid} 没有动作: {name}")
        return run_with_timeout(handler, dict(arguments or {}), timeout=timeout)

    def _load_module(self, manifest: PluginManifest) -> Any:
        try:
            entry_path = manifest.entry_path
        except safety.UnsafePathError as exc:
            raise PluginLoadError(f"入口路径非法: {exc}") from exc
        if not entry_path or not os.path.isfile(entry_path):
            raise PluginLoadError(f"入口文件不存在: {manifest.entry}")

        module_name = self._module_name(manifest.id)
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"无法加载入口模块: {manifest.entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        plugin_dir = os.path.realpath(manifest.plugin_dir)
        sys.path.insert(0, plugin_dir)
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        finally:
            try:
                sys.path.remove(plugin_dir)
            except ValueError:
                pass
        return module

    def disable(self, plugin_id: str) -> Dict[str, Any]:
        """停用插件：调用 ``unregister(ctx)`` 并回收全部注册项。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            self._ensure_scan()
            error = self._deactivate(pid)
            fields: Dict[str, Any] = {"enabled": False, "enabled_at": None}
            if error:
                fields["last_error"] = error
            self._write_state(pid, **fields)
            if pid not in self._manifests:
                raise PluginNotFound(f"插件不存在: {pid}")
            self._refresh_core_tool()
            return self._detail(pid)

    def _deactivate(self, plugin_id: str) -> Optional[str]:
        module = self._modules.pop(plugin_id, None)
        ctx = self._contexts.pop(plugin_id, None)
        host = self._hosts.pop(plugin_id, None)
        error: Optional[str] = None
        if module is not None:
            unregister = getattr(module, "unregister", None)
            if callable(unregister):
                try:
                    unregister(ctx)
                except Exception as exc:
                    error = str(exc)
                    _log.warning("[Plugins] unregister(%s) 抛错: %s", plugin_id, exc)
        if host is not None:
            try:
                host.close()
            except Exception as exc:
                _log.debug("[Plugins] 关闭插件宿主 %s 失败: %s", plugin_id, exc)
        try:
            self._registry.unregister_source(plugin_id)
        except Exception as exc:
            _log.warning("[Plugins] 回收插件 %s 注册资源失败: %s", plugin_id, exc)
        sys.modules.pop(self._module_name(plugin_id), None)
        return error

    def reload(self, plugin_id: str) -> Dict[str, Any]:
        """热重载：重新读 manifest 并重新注册；失败时尽力恢复旧模块。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            self.ensure_bootstrapped()
            self._ensure_scan()
            old_module = self._modules.get(pid)
            old_ctx = self._contexts.get(pid)
            self._deactivate(pid)

            try:
                manifest = load_manifest(os.path.join(self._root, pid))
            except ManifestError as exc:
                self._write_state(pid, enabled=False, last_error=str(exc))
                raise PluginLoadError(f"重载失败（manifest 非法）: {exc}") from exc
            self._manifests[pid] = manifest

            state = self._state_view(pid)
            guard = PermissionGuard(pid, manifest.permissions, state["permissions_override"])
            try:
                settings = normalize_settings(manifest, state["settings"])
            except ManifestError as exc:
                self._write_state(pid, enabled=False, last_error=str(exc))
                raise PluginLoadError(f"重载失败（设置值非法）: {exc}") from exc

            error = self._activate(manifest, guard, settings)
            if error:
                self._write_state(pid, enabled=False, last_error=error)
                if old_module is not None and old_ctx is not None:
                    if self._restore_module(pid, old_module, old_ctx):
                        self._write_state(pid, enabled=True, last_error=None)
                        self._refresh_core_tool()
                        raise PluginLoadError(f"重载失败，已回滚旧版本: {error}")
                raise PluginLoadError(f"重载失败: {error}")

            self._write_state(
                pid,
                enabled=True,
                version=manifest.version,
                enabled_at=time.time(),
                last_error=None,
            )
            self._refresh_core_tool()
            return self._detail(pid)

    def _restore_module(
        self, plugin_id: str, module: Any, ctx: PluginContext
    ) -> bool:
        """重载失败后尝试用旧模块对象重新注册。"""
        try:
            register = getattr(module, "register", None)
            if not callable(register):
                return False
            host = PluginHost(
                plugin_id=plugin_id,
                manifest=ctx.manifest,
                settings=dict(ctx.settings),
                guard=ctx.guard,
                registry=self._registry,
                plugin_dir=ctx.manifest.plugin_dir,
                data_dir=os.path.join(self._root, ".data"),
                services=self._services,
                logger=ctx.log or logging.getLogger(f"nbot.plugin.{plugin_id}"),
            )
            ctx.host = host
            ctx.api = host.api
            self._hosts[plugin_id] = host
            sys.modules[self._module_name(plugin_id)] = module
            register(ctx)
            self._modules[plugin_id] = module
            self._contexts[plugin_id] = ctx
            _log.info("[Plugins] 已回滚到旧版本: %s", plugin_id)
            return True
        except Exception as exc:
            _log.warning("[Plugins] 回滚旧版本失败 %s: %s", plugin_id, exc)
            self._deactivate(plugin_id)
            return False

    def uninstall(
        self, plugin_id: str, *, keep_state: bool = False, delete_data: bool = False
    ) -> Dict[str, Any]:
        """卸载插件：停用后删除目录。

        - ``keep_state=True``：保留启用状态/设置记录（默认删除）；
        - ``delete_data=True``：同时删除插件私有 KV 与日志（``data/plugins/.data/<id>``）。
        """
        pid = self._require_id(plugin_id)
        with self._lock:
            self._ensure_scan()
            root_real = os.path.realpath(self._root)
            try:
                target = safety.resolve_inside(self._root, pid)
            except safety.UnsafePathError as exc:
                raise PluginStateError(f"插件 id 非法: {exc}") from exc
            expected = os.path.join(root_real, pid)
            if os.path.normcase(os.path.realpath(target)) != os.path.normcase(expected):
                raise PluginStateError(f"拒绝删除非插件目录: {target}")

            self._deactivate(pid)
            self._manifests.pop(pid, None)
            if os.path.isdir(target):
                shutil.rmtree(target)
            self._scanned = True

            data_removed = False
            if delete_data:
                data_root = safety.resolve_inside(self._root, os.path.join(".data", pid))
                expected_data = os.path.join(root_real, ".data", pid)
                if os.path.normcase(os.path.realpath(data_root)) == os.path.normcase(
                    expected_data
                ) and os.path.isdir(data_root):
                    shutil.rmtree(data_root)
                    data_removed = True

            if keep_state:
                self._write_state(pid, enabled=False, enabled_at=None)
            else:
                self._load_state()
                (self._state.get("plugins") or {}).pop(pid, None)
                self._save_state()
            self._refresh_core_tool()
            return {
                "id": pid,
                "removed": not os.path.isdir(target),
                "state_kept": keep_state,
                "data_removed": data_removed,
            }

    # -- 设置与权限 -------------------------------------------------------

    def update_settings(self, plugin_id: str, values: Any) -> Dict[str, Any]:
        """更新插件设置（按 settings_schema 归一化，未声明键直接报错）。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            manifest = self._manifest(pid)
            normalized = normalize_settings(manifest, values)
            self._write_state(pid, settings=normalized)
            ctx = self._contexts.get(pid)
            if ctx is not None:
                ctx.settings.clear()
                ctx.settings.update(normalized)
            return self._detail(pid)

    def update_permissions(
        self, plugin_id: str, permissions: Optional[Iterable[str]]
    ) -> Dict[str, Any]:
        """设置管理员权限策略（只能从声明里收紧）；传 None 表示恢复声明全量。"""
        pid = self._require_id(plugin_id)
        with self._lock:
            manifest = self._manifest(pid)
            if permissions is None:
                override: Optional[List[str]] = None
                guard_permissions: Optional[List[str]] = None
            else:
                effective = effective_permissions(manifest.permissions, permissions)
                override = list(effective)
                guard_permissions = override
            self._write_state(pid, permissions_override=override)
            ctx = self._contexts.get(pid)
            if ctx is not None and ctx.guard is not None:
                ctx.guard.apply_override(guard_permissions)
            return self._detail(pid)

    # -- 宿主服务与核心工具 -----------------------------------------------

    def configure_services(self, services: HostServices) -> None:
        """注入宿主服务（会话/消息/工作区/事件总线），并同步到已加载插件。"""
        with self._lock:
            self._services = services
            for host in self._hosts.values():
                host.services = services

    def enabled_plugin_summaries(self) -> List[Dict[str, Any]]:
        """已启用（已加载）插件的摘要，供 ``plugin_use`` 动态描述使用。"""
        with self._lock:
            self._ensure_scan()
            return [self._detail(pid) for pid in sorted(self._modules)]

    def _refresh_core_tool(self) -> None:
        """按当前启用插件刷新 ``plugin_use`` 工具（无插件时注销）。"""
        try:
            from nbot.plugins.plugin_use import refresh_plugin_use_tool
        except Exception as exc:  # 模块缺失不应影响插件生命周期
            _log.debug("[Plugins] plugin_use 模块不可用: %s", exc)
            return
        try:
            refresh_plugin_use_tool(self)
        except Exception as exc:
            _log.warning("[Plugins] 刷新 plugin_use 失败: %s", exc)


_runtime: Optional[PluginRuntime] = None
_runtime_lock = threading.Lock()


def get_plugin_runtime() -> PluginRuntime:
    """获取插件运行时单例。"""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = PluginRuntime()
        return _runtime


def reset_plugin_runtime() -> None:
    """重置单例（测试用）。"""
    global _runtime
    with _runtime_lock:
        _runtime = None
