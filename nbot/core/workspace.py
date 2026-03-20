"""
工作区管理模块

每个会话（QQ私聊/群聊、Web会话）对应一个独立的工作区文件夹。
用户上传的文件存入工作区，AI 也可以在工作区中创建、读取、修改、删除文件。
删除会话时同步删除对应的工作区文件夹。
"""

import os
import json
import shutil
import logging
import mimetypes
from typing import Optional, List, Dict, Any
from datetime import datetime

_log = logging.getLogger(__name__)


class WorkspaceManager:
    """工作区管理器（单例模式）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.workspaces_dir = os.path.join(self.base_dir, 'data', 'workspaces')
        self.meta_file = os.path.join(self.workspaces_dir, '_meta.json')

        os.makedirs(self.workspaces_dir, exist_ok=True)

        # session_id -> workspace_path 的映射
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._load_meta()

    # ========== 元数据管理 ==========

    def _load_meta(self):
        """加载工作区元数据"""
        if os.path.exists(self.meta_file):
            try:
                with open(self.meta_file, 'r', encoding='utf-8') as f:
                    self._meta = json.load(f)
            except Exception as e:
                _log.error(f"加载工作区元数据失败: {e}")
                self._meta = {}

    def _save_meta(self):
        """保存工作区元数据"""
        try:
            with open(self.meta_file, 'w', encoding='utf-8') as f:
                json.dump(self._meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.error(f"保存工作区元数据失败: {e}")

    # ========== 工作区生命周期 ==========

    def get_or_create(self, session_id: str, session_type: str = "unknown",
                      session_name: str = "") -> str:
        """
        获取或创建会话对应的工作区目录

        Args:
            session_id: 会话唯一标识
            session_type: 会话类型 (qq_private, qq_group, web, workflow)
            session_name: 会话名称（用于元数据记录）

        Returns:
            工作区绝对路径
        """
        if session_id in self._meta:
            ws_path = self._meta[session_id]['path']
            if os.path.isdir(ws_path):
                return ws_path
            # 目录被外部删除了，重新创建
            os.makedirs(ws_path, exist_ok=True)
            return ws_path

        # 创建新工作区
        # 根据 session_type 生成更有区分度的文件夹名
        if session_type.startswith('qq_'):
            # QQ 会话：提取 QQ 号作为标识
            # session_id 格式: qq_private_{qq_id} 或 qq_group_{group_id} 或 qq_group_{group_id}_{user_id}
            parts = session_id.split('_')
            if len(parts) >= 3:
                # 提取最后一部分（QQ号或群号）
                qq_id = parts[-1]
                folder_name = f"{session_type}_{qq_id}"
            else:
                # 备用方案：使用 hash
                import hashlib
                short_hash = hashlib.md5(session_id.encode()).hexdigest()[:8]
                folder_name = f"{session_type}_{short_hash}"
        else:
            # Web 会话：使用 session_id 前8位
            short_id = session_id.replace('-', '')[:8]
            folder_name = f"{session_type}_{short_id}"
        
        # 清理文件夹名中的非法字符
        folder_name = folder_name.replace(':', '_').replace('/', '_').replace('\\', '_')
        
        ws_path = os.path.join(self.workspaces_dir, folder_name)
        os.makedirs(ws_path, exist_ok=True)

        self._meta[session_id] = {
            'path': ws_path,
            'folder': folder_name,
            'type': session_type,
            'name': session_name,
            'created_at': datetime.now().isoformat()
        }
        self._save_meta()

        _log.info(f"创建工作区: {folder_name} (session={session_id[:8]}...)")
        return ws_path

    def get_workspace(self, session_id: str) -> Optional[str]:
        """
        获取会话对应的工作区路径（不自动创建）

        Returns:
            工作区路径，不存在则返回 None
        """
        info = self._meta.get(session_id)
        if info and os.path.isdir(info['path']):
            return info['path']
        return None

    def delete_workspace(self, session_id: str) -> bool:
        """
        删除会话对应的工作区

        Returns:
            是否成功删除
        """
        info = self._meta.get(session_id)
        if not info:
            return False

        ws_path = info['path']
        try:
            if os.path.isdir(ws_path):
                shutil.rmtree(ws_path)
                _log.info(f"删除工作区: {info['folder']} (session={session_id[:8]}...)")
        except Exception as e:
            _log.error(f"删除工作区失败: {ws_path}, {e}")
            return False

        del self._meta[session_id]
        self._save_meta()
        return True

    # ========== 文件操作 ==========

    def save_uploaded_file(self, session_id: str, file_data: bytes,
                           filename: str, session_type: str = "unknown") -> Dict[str, Any]:
        """
        保存用户上传的文件到工作区

        Args:
            session_id: 会话ID
            file_data: 文件二进制内容
            filename: 原始文件名
            session_type: 会话类型

        Returns:
            文件信息字典
        """
        ws_path = self.get_or_create(session_id, session_type)
        safe_name = self._safe_filename(filename)
        file_path = os.path.join(ws_path, safe_name)

        # 同名文件自动加序号
        if os.path.exists(file_path):
            base, ext = os.path.splitext(safe_name)
            i = 1
            while os.path.exists(file_path):
                file_path = os.path.join(ws_path, f"{base}_{i}{ext}")
                i += 1

        with open(file_path, 'wb') as f:
            f.write(file_data)

        actual_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)

        _log.info(f"文件已保存到工作区: {actual_name} (session={session_id[:8]}...)")
        return {
            'success': True,
            'filename': actual_name,
            'path': file_path,
            'size': len(file_data),
            'mime_type': mime_type or 'application/octet-stream'
        }

    def save_uploaded_file_from_path(self, session_id: str, src_path: str,
                                      filename: str = None,
                                      session_type: str = "unknown") -> Dict[str, Any]:
        """
        从本地路径复制文件到工作区

        Args:
            session_id: 会话ID
            src_path: 源文件路径
            filename: 目标文件名（默认使用源文件名）
            session_type: 会话类型

        Returns:
            文件信息字典
        """
        if not os.path.exists(src_path):
            return {'success': False, 'error': f'源文件不存在: {src_path}'}

        ws_path = self.get_or_create(session_id, session_type)
        if not filename:
            filename = os.path.basename(src_path)
        safe_name = self._safe_filename(filename)
        dest_path = os.path.join(ws_path, safe_name)

        # 同名文件自动加序号
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(safe_name)
            i = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(ws_path, f"{base}_{i}{ext}")
                i += 1

        shutil.copy2(src_path, dest_path)
        actual_name = os.path.basename(dest_path)
        mime_type, _ = mimetypes.guess_type(dest_path)

        return {
            'success': True,
            'filename': actual_name,
            'path': dest_path,
            'size': os.path.getsize(dest_path),
            'mime_type': mime_type or 'application/octet-stream'
        }

    def create_file(self, session_id: str, filename: str, content: str,
                    session_type: str = "unknown") -> Dict[str, Any]:
        """
        在工作区中创建/覆盖文本文件（供 AI 工具调用）

        Args:
            session_id: 会话ID
            filename: 文件名
            content: 文件内容

        Returns:
            操作结果
        """
        ws_path = self.get_or_create(session_id, session_type)
        safe_name = self._safe_filename(filename)

        # 支持子目录，但限制在工作区内
        file_path = os.path.normpath(os.path.join(ws_path, safe_name))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法，不能超出工作区范围'}

        # 确保父目录存在
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return {
                'success': True,
                'filename': safe_name,
                'path': file_path,
                'size': len(content.encode('utf-8'))
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def read_file(self, session_id: str, filename: str) -> Dict[str, Any]:
        """
        读取工作区中的文件内容（供 AI 工具调用）

        Args:
            session_id: 会话ID
            filename: 文件名

        Returns:
            文件内容或错误信息
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': False, 'error': '工作区不存在'}

        file_path = os.path.normpath(os.path.join(ws_path, filename))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        mime_type, _ = mimetypes.guess_type(file_path)
        is_text = mime_type and (
            mime_type.startswith('text/') or
            mime_type in ['application/json', 'application/xml', 'application/yaml',
                          'application/javascript', 'application/x-python']
        )

        # 对于无法识别 mime 的文件，尝试按文本读取
        if not mime_type:
            is_text = True

        if is_text:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                # 限制返回大小
                if len(content) > 50000:
                    content = content[:50000] + "\n...(内容过长，已截断)"
                return {
                    'success': True,
                    'filename': filename,
                    'content': content,
                    'size': os.path.getsize(file_path),
                    'mime_type': mime_type or 'text/plain'
                }
            except Exception:
                pass

        # 二进制文件只返回元信息
        return {
            'success': True,
            'filename': filename,
            'content': f'[二进制文件，大小: {os.path.getsize(file_path)} 字节]',
            'size': os.path.getsize(file_path),
            'mime_type': mime_type or 'application/octet-stream',
            'is_binary': True
        }

    def edit_file(self, session_id: str, filename: str,
                  old_content: str, new_content: str) -> Dict[str, Any]:
        """
        修改工作区中的文件（查找替换方式，供 AI 工具调用）

        Args:
            session_id: 会话ID
            filename: 文件名
            old_content: 要替换的原始内容
            new_content: 替换后的新内容

        Returns:
            操作结果
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': False, 'error': '工作区不存在'}

        file_path = os.path.normpath(os.path.join(ws_path, filename))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if old_content not in content:
                return {'success': False, 'error': '未找到要替换的内容'}

            new_file_content = content.replace(old_content, new_content, 1)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_file_content)

            return {
                'success': True,
                'filename': filename,
                'size': len(new_file_content.encode('utf-8'))
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def delete_file(self, session_id: str, filename: str) -> Dict[str, Any]:
        """
        删除工作区中的文件（供 AI 工具调用）
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': False, 'error': '工作区不存在'}

        file_path = os.path.normpath(os.path.join(ws_path, filename))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        try:
            os.remove(file_path)
            return {'success': True, 'filename': filename}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def list_files(self, session_id: str) -> Dict[str, Any]:
        """
        列出工作区中的所有文件（供 AI 工具调用）
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': True, 'files': [], 'message': '工作区为空'}

        files = []
        try:
            for root, dirs, filenames in os.walk(ws_path):
                for fname in filenames:
                    full_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(full_path, ws_path)
                    mime_type, _ = mimetypes.guess_type(full_path)
                    files.append({
                        'name': rel_path.replace('\\', '/'),
                        'size': os.path.getsize(full_path),
                        'mime_type': mime_type or 'application/octet-stream',
                        'modified': datetime.fromtimestamp(
                            os.path.getmtime(full_path)
                        ).isoformat()
                    })
        except Exception as e:
            return {'success': False, 'error': str(e)}

        return {'success': True, 'files': files, 'count': len(files)}

    def get_file_path(self, session_id: str, filename: str) -> Optional[str]:
        """
        获取工作区中文件的绝对路径（用于发送文件给用户）

        Returns:
            文件绝对路径，不存在则返回 None
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return None

        file_path = os.path.normpath(os.path.join(ws_path, filename))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return None

        if os.path.exists(file_path):
            return file_path
        return None

    # ========== 工具方法 ==========

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """清理文件名，移除危险字符"""
        # 保留子目录分隔符
        parts = filename.replace('\\', '/').split('/')
        safe_parts = []
        for part in parts:
            # 移除路径遍历
            if part in ('.', '..', ''):
                continue
            # 移除特殊字符
            safe = ''.join(c for c in part if c.isalnum() or c in '._- ()')
            if safe:
                safe_parts.append(safe)
        return '/'.join(safe_parts) if safe_parts else 'unnamed_file'

    def get_all_workspaces(self) -> List[Dict[str, Any]]:
        """获取所有工作区信息"""
        result = []
        for session_id, info in self._meta.items():
            ws_path = info['path']
            file_count = 0
            total_size = 0
            if os.path.isdir(ws_path):
                for root, dirs, files in os.walk(ws_path):
                    file_count += len(files)
                    for f in files:
                        total_size += os.path.getsize(os.path.join(root, f))
            result.append({
                'session_id': session_id,
                'folder': info.get('folder', ''),
                'type': info.get('type', 'unknown'),
                'name': info.get('name', ''),
                'created_at': info.get('created_at', ''),
                'file_count': file_count,
                'total_size': total_size
            })
        return result


# 全局单例
workspace_manager = WorkspaceManager()
