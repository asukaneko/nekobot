"""工作区检索与网页抓取工具

补充 ``workspace_*`` 之外的三类"读取型"能力（对标 Android v0.7.1 的工具集）：

- ``search_files``：在工作区里按正则搜索文件内容（类似 grep）
- ``find_files``：按文件名/相对路径通配符找文件（类似 glob）
- ``web_fetch``：抓取一个 http(s) 页面并转成纯文本

所有工具遵守工作区隔离：路径解析后必须仍在目标工作区根目录内。
"""

import fnmatch
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# 跳过的大目录与二进制后缀
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".rar", ".7z", ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".avi",
    ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll", ".exe", ".pyc", ".class",
    ".bin", ".db", ".sqlite", ".sqlite3",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_MATCHES = 50
MAX_MATCHES_LIMIT = 200
DEFAULT_MAX_FILES = 100
MAX_FILES_LIMIT = 500
MAX_LINE_CHARS = 300
SNIPPET_CHARS = 320


def _resolve_root(context: Dict[str, Any], scope: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 (工作区根目录, 错误信息)。"""
    try:
        from nbot.core.workspace import workspace_manager
    except ImportError:
        return None, "工作区模块不可用"

    ctx = context or {}
    session_id = str(ctx.get("session_id") or "").strip()
    session_type = str(ctx.get("session_type") or "unknown")
    if scope == "shared":
        try:
            root = workspace_manager.get_shared_workspace()
        except Exception as exc:  # pragma: no cover - 依赖具体实现
            return None, f"无法获取共享工作区: {exc}"
    else:
        if not session_id:
            return None, "缺少会话信息，无法操作工作区"
        try:
            root = workspace_manager.get_or_create(session_id, session_type)
        except Exception as exc:  # pragma: no cover
            return None, f"无法获取工作区: {exc}"
    if not root:
        return None, "工作区尚未初始化"
    return os.path.abspath(root), None


def _safe_join(root: str, rel_path: str) -> Tuple[Optional[str], Optional[str]]:
    """把相对路径安全地拼到工作区根目录，越界返回错误。"""
    rel_path = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    target = os.path.abspath(os.path.join(root, rel_path)) if rel_path else root
    try:
        if os.path.commonpath([root, target]) != root:
            return None, "路径越界：只能访问当前工作区内的文件"
    except ValueError:
        return None, "路径无效"
    return target, None


def _iter_files(target: str, *, follow_root_only: bool = True):
    """遍历目录下的文件（跳过缓存目录与符号链接）。"""
    if os.path.isfile(target):
        yield target
        return
    for current, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git")]
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            yield path


def _is_probably_text(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in BINARY_EXTENSIONS:
        return False
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def search_files(arguments: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """在工作区文件中按正则搜索内容。"""
    pattern = str(arguments.get("pattern") or "").strip()
    if not pattern:
        return {"success": False, "error": "缺少 pattern 参数（正则表达式）"}

    scope = str(arguments.get("scope") or "private")
    root, error = _resolve_root(context, scope)
    if error:
        return {"success": False, "error": error}

    sub_path = str(arguments.get("path") or "").strip()
    target, error = _safe_join(root, sub_path)
    if error:
        return {"success": False, "error": error}
    if not os.path.exists(target):
        return {"success": False, "error": f"路径不存在: {sub_path or '.'}"}

    try:
        flags = 0 if arguments.get("case_sensitive") else re.IGNORECASE
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return {"success": False, "error": f"正则表达式无效: {exc}"}

    file_glob = str(arguments.get("glob") or "").strip()
    max_matches = int(arguments.get("max_results") or DEFAULT_MAX_MATCHES)
    max_matches = max(1, min(MAX_MATCHES_LIMIT, max_matches))
    context_lines = max(0, min(5, int(arguments.get("context_lines") or 0)))

    matches: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False

    for path in _iter_files(target):
        if not _is_probably_text(path):
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        if file_glob and not (
            fnmatch.fnmatch(rel, file_glob) or fnmatch.fnmatch(os.path.basename(path), file_glob)
        ):
            continue
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "…"
            if not regex.search(line):
                continue
            item: Dict[str, Any] = {
                "file": rel,
                "line": index + 1,
                "text": line.strip()[:MAX_LINE_CHARS],
            }
            if context_lines:
                start = max(0, index - context_lines)
                end = min(len(lines), index + context_lines + 1)
                item["context"] = [ln[:MAX_LINE_CHARS] for ln in lines[start:end]]
            matches.append(item)
            if len(matches) >= max_matches:
                truncated = True
                break
        if truncated:
            break

    return {
        "success": True,
        "scope": scope,
        "pattern": pattern,
        "scanned_files": scanned,
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches,
        "summary": f"在 {scanned} 个文件中找到 {len(matches)} 处匹配"
        + ("（已达上限，请缩小范围或提高 max_results）" if truncated else ""),
    }


def find_files(arguments: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """按通配符查找文件（支持 ``*.py``、``docs/**/*.md`` 这类模式）。"""
    pattern = str(arguments.get("pattern") or "").strip() or "*"
    scope = str(arguments.get("scope") or "private")
    root, error = _resolve_root(context, scope)
    if error:
        return {"success": False, "error": error}

    sub_path = str(arguments.get("path") or "").strip()
    target, error = _safe_join(root, sub_path)
    if error:
        return {"success": False, "error": error}
    if not os.path.exists(target):
        return {"success": False, "error": f"路径不存在: {sub_path or '.'}"}

    max_files = int(arguments.get("max_results") or DEFAULT_MAX_FILES)
    max_files = max(1, min(MAX_FILES_LIMIT, max_files))
    name_only = bool(arguments.get("name_only"))

    found: List[Dict[str, Any]] = []
    truncated = False
    for path in _iter_files(target):
        rel = os.path.relpath(path, root).replace("\\", "/")
        candidate = os.path.basename(path) if name_only else rel
        if not fnmatch.fnmatch(candidate, pattern):
            if not (pattern.startswith("*") and fnmatch.fnmatch(candidate, f"*{pattern}")):
                continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        found.append({"path": rel, "size": size})
        if len(found) >= max_files:
            truncated = True
            break

    found.sort(key=lambda item: item["path"])
    return {
        "success": True,
        "scope": scope,
        "pattern": pattern,
        "count": len(found),
        "truncated": truncated,
        "files": found,
        "summary": f"匹配到 {len(found)} 个文件"
        + ("（已达上限）" if truncated else ""),
    }


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:  # pragma: no cover - bs4 可选
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def web_fetch(arguments: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
    """抓取网页并转成纯文本（GET，仅 http/https）。"""
    url = str(arguments.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "缺少 url 参数"}
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"success": False, "error": "仅支持 http/https 链接"}

    max_chars = int(arguments.get("max_chars") or 20000)
    max_chars = max(500, min(200000, max_chars))
    raw = bool(arguments.get("raw"))

    try:
        import requests

        response = requests.get(
            url,
            timeout=float(arguments.get("timeout") or 20),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NekoBot/1.0"
                )
            },
            allow_redirects=True,
        )
    except Exception as exc:
        return {"success": False, "error": f"抓取失败: {exc}"}

    content_type = response.headers.get("Content-Type", "")
    if response.status_code >= 400:
        return {
            "success": False,
            "error": f"HTTP {response.status_code}",
            "url": response.url,
        }

    body = response.text or ""
    text = body if raw else _html_to_text(body) if "html" in content_type.lower() else body
    truncated = len(text) > max_chars
    text = text[:max_chars]

    return {
        "success": True,
        "url": response.url,
        "status_code": response.status_code,
        "content_type": content_type,
        "truncated": truncated,
        "length": len(body),
        "text": text,
    }


SEARCH_TOOL_NAMES = ("search_files", "find_files", "web_fetch")


def get_search_tool_schemas() -> List[Dict[str, Any]]:
    """返回三个工具的 OpenAI function-calling schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": (
                    "在工作区文件中按正则表达式搜索内容（类似 grep）。返回命中的文件、行号与整行文本，"
                    "可选返回上下文行。适用于在代码/文档中定位关键词、函数名或报错信息。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "正则表达式，例如 'def main' 或 'TODO|FIXME'",
                        },
                        "path": {
                            "type": "string",
                            "description": "限定搜索的子目录或文件（可选），如 'docs' 或 'src/app.py'",
                        },
                        "glob": {
                            "type": "string",
                            "description": "只搜索匹配的文件（可选），如 '*.py' 或 'docs/**/*.md'",
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["private", "shared"],
                            "description": "workspace 范围，默认 private",
                        },
                        "case_sensitive": {"type": "boolean", "description": "是否区分大小写"},
                        "context_lines": {
                            "type": "integer",
                            "description": "每条命中附带的上下文行数（0-5）",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": f"最多返回多少条命中，默认 {DEFAULT_MAX_MATCHES}",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_files",
                "description": (
                    "按文件名/相对路径通配符查找工作区文件（类似 glob），返回路径与大小。"
                    "适用于先看目录结构再决定读哪个文件。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "通配符，如 '*.md'、'docs/**/test_*.py'",
                        },
                        "path": {"type": "string", "description": "限定起始目录（可选）"},
                        "scope": {
                            "type": "string",
                            "enum": ["private", "shared"],
                            "description": "workspace 范围，默认 private",
                        },
                        "name_only": {
                            "type": "boolean",
                            "description": "true 时只匹配文件名（忽略目录层级）",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": f"最多返回多少个文件，默认 {DEFAULT_MAX_FILES}",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": (
                    "抓取指定 http(s) 网页并返回纯文本（HTML 会自动去标签）。"
                    "适用于读取用户给的链接、文档页或 API 返回。单次最多返回 max_chars 字符。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要抓取的 http(s) 地址"},
                        "max_chars": {
                            "type": "integer",
                            "description": "最多返回多少字符，默认 20000",
                        },
                        "raw": {
                            "type": "boolean",
                            "description": "true 时返回原始响应体（不转纯文本）",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    ]


def execute_search_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    context: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """分发三个检索类工具。"""
    arguments = arguments or {}
    context = context or {}
    if tool_name == "search_files":
        return search_files(arguments, context)
    if tool_name == "find_files":
        return find_files(arguments, context)
    if tool_name == "web_fetch":
        return web_fetch(arguments, context)
    return {"success": False, "error": f"未知检索工具: {tool_name}"}
