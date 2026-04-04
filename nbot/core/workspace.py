"""
工作区管理模块

每个会话（QQ私聊/群聊、Web会话）对应一个独立的工作区文件夹。
用户上传的文件存入工作区，AI 也可以在工作区中创建、读取、修改、删除文件。
删除会话时同步删除对应的工作区文件夹。

环境变量:
    NBOT_WORKSPACE_DIR: 自定义工作区根目录路径（可选）
                        如果不设置，默认使用项目目录下的 data/workspaces
"""

import os
import json
import shutil
import logging
import mimetypes
from typing import Optional, List, Dict, Any
from datetime import datetime

_log = logging.getLogger(__name__)


def _normalize_edit_block(text: str) -> str:
    return "\n".join(
        line.rstrip() for line in (text or "").replace("\r\n", "\n").strip().split("\n")
    )


def _replace_content_block(content: str, old_content: str, new_content: str):
    if old_content in content:
        return content.replace(old_content, new_content, 1), "exact"

    normalized_content = content.replace("\r\n", "\n")
    normalized_old = (old_content or "").replace("\r\n", "\n")
    if normalized_old and normalized_old in normalized_content:
        return normalized_content.replace(normalized_old, new_content, 1), "normalized_newlines"

    relaxed_old = _normalize_edit_block(old_content)
    if not relaxed_old:
        return None, None

    content_lines = content.replace("\r\n", "\n").split("\n")
    old_lines = relaxed_old.split("\n")
    old_len = len(old_lines)
    for start in range(0, len(content_lines) - old_len + 1):
        candidate = "\n".join(content_lines[start : start + old_len])
        if _normalize_edit_block(candidate) == relaxed_old:
            new_lines = new_content.replace("\r\n", "\n").split("\n")
            updated_lines = content_lines[:start] + new_lines + content_lines[start + old_len :]
            return "\n".join(updated_lines), "relaxed_block"

    return None, None


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

        # 优先从环境变量获取工作区目录
        workspace_env = os.environ.get('NBOT_WORKSPACE_DIR')

        if workspace_env:
            # 使用环境变量指定的路径
            self.workspaces_dir = os.path.normpath(workspace_env)
            _log.info(f"使用环境变量指定的工作区目录: {self.workspaces_dir}")
        else:
            # 默认使用项目目录下的 data/workspaces
            self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            self.workspaces_dir = os.path.join(self.base_dir, 'data', 'workspaces')
            _log.info(f"使用默认工作区目录: {self.workspaces_dir}")

        self.meta_file = os.path.join(self.workspaces_dir, '_meta.json')

        # 确保工作区目录存在
        try:
            os.makedirs(self.workspaces_dir, exist_ok=True)
            _log.info(f"工作区目录已就绪: {self.workspaces_dir}")
        except Exception as e:
            _log.error(f"创建工作区目录失败: {e}")
            # 如果创建失败，使用临时目录作为后备
            import tempfile
            self.workspaces_dir = os.path.join(tempfile.gettempdir(), 'nbot_workspaces')
            self.meta_file = os.path.join(self.workspaces_dir, '_meta.json')
            os.makedirs(self.workspaces_dir, exist_ok=True)
            _log.warning(f"使用临时目录作为工作区: {self.workspaces_dir}")

        # session_id -> workspace_path 的映射
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._load_meta()

        # 共享工作区（所有会话共用）
        self.shared_workspace_dir = os.path.join(self.workspaces_dir, '_shared')
        try:
            os.makedirs(self.shared_workspace_dir, exist_ok=True)
            _log.info(f"共享工作区目录已就绪: {self.shared_workspace_dir}")
        except Exception as e:
            _log.error(f"创建共享工作区目录失败: {e}")

    # ========== 共享工作区 ==========

    def get_shared_workspace(self) -> str:
        """获取共享工作区路径（所有会话共用）"""
        return self.shared_workspace_dir

    def list_shared_files(self, path: str = '') -> Dict[str, Any]:
        """列出共享工作区中的文件"""
        target_path = os.path.join(self.shared_workspace_dir, path) if path else self.shared_workspace_dir

        if not os.path.exists(target_path) or not os.path.isdir(target_path):
            return {'success': False, 'error': '目录不存在'}

        files = []
        try:
            for name in os.listdir(target_path):
                full_path = os.path.join(target_path, name)
                if os.path.isdir(full_path):
                    files.append({
                        'name': name,
                        'type': 'directory',
                        'size': 0,
                        'path': path + '/' + name if path else name,
                        'scope': 'shared'
                    })
                else:
                    mime_type, _ = mimetypes.guess_type(full_path)
                    files.append({
                        'name': name,
                        'type': 'file',
                        'size': os.path.getsize(full_path),
                        'mime_type': mime_type or 'application/octet-stream',
                        'path': path + '/' + name if path else name,
                        'scope': 'shared'
                    })
        except Exception as e:
            return {'success': False, 'error': str(e)}

        return {'success': True, 'files': files, 'count': len(files), 'scope': 'shared'}

    def list_shared_files_recursive(self, path: str = '') -> Dict[str, Any]:
        """递归列出共享工作区中所有子目录的文件"""
        target_path = os.path.join(self.shared_workspace_dir, path) if path else self.shared_workspace_dir

        if not os.path.exists(target_path) or not os.path.isdir(target_path):
            return {'success': False, 'error': '目录不存在'}

        all_items = []
        
        def scan_directory(current_path: str, relative_prefix: str = ''):
            try:
                for name in os.listdir(current_path):
                    full_path = os.path.join(current_path, name)
                    relative_path = relative_prefix + '/' + name if relative_prefix else name
                    
                    if os.path.isdir(full_path):
                        # 先添加文件夹，然后递归扫描其内容
                        all_items.append({
                            'name': name,
                            'type': 'directory',
                            'size': 0,
                            'path': relative_path,
                            'scope': 'shared'
                        })
                        scan_directory(full_path, relative_path)
                    else:
                        mime_type, _ = mimetypes.guess_type(full_path)
                        all_items.append({
                            'name': name,
                            'type': 'file',
                            'size': os.path.getsize(full_path),
                            'mime_type': mime_type or 'application/octet-stream',
                            'path': relative_path,
                            'scope': 'shared'
                        })
            except Exception:
                pass
        
        scan_directory(target_path, path)
        
        return {
            'success': True,
            'files': all_items,
            'count': len(all_items),
            'scope': 'shared',
            'recursive': True
        }

    def read_shared_file(self, filename: str, start_line: int = None, end_line: int = None,
                         char_count: int = None, start_char: int = None) -> Dict[str, Any]:
        """读取共享工作区中的文件"""
        file_path = os.path.normpath(os.path.join(self.shared_workspace_dir, filename))
        if not file_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        if os.path.isdir(file_path):
            return {'success': False, 'error': f'这是一个文件夹，不是文件: {filename}'}

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 应用行范围限制
            if start_line is not None or end_line is not None:
                lines = content.split('\n')
                start = start_line - 1 if start_line else 0
                end = end_line if end_line else len(lines)
                content = '\n'.join(lines[start:end])

            # 应用字符范围限制
            if start_char is not None or char_count is not None:
                start = start_char if start_char else 0
                end = start + char_count if char_count else len(content)
                content = content[start:end]

            return {
                'success': True,
                'filename': filename,
                'content': content,
                'size': len(content),
                'scope': 'shared'
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def create_shared_folder(self, folder_name: str, path: str = '') -> Dict[str, Any]:
        """在共享工作区创建文件夹"""
        if not folder_name.strip():
            return {'success': False, 'error': '文件夹名称不能为空'}

        import re
        if not re.match(r'^[\w\-\. ]+$', folder_name):
            return {'success': False, 'error': '文件夹名称包含非法字符'}

        if path:
            # 检查路径是否在共享工作区内
            check_path = os.path.normpath(os.path.join(self.shared_workspace_dir, path))
            if not check_path.startswith(os.path.normpath(self.shared_workspace_dir)):
                return {'success': False, 'error': '路径不合法，不能超出共享工作区'}
            folder_path = os.path.join(self.shared_workspace_dir, path, folder_name)
        else:
            folder_path = os.path.join(self.shared_workspace_dir, folder_name)

        try:
            if os.path.exists(folder_path):
                return {'success': False, 'error': '文件夹已存在'}

            os.makedirs(folder_path, exist_ok=True)
            return {'success': True, 'name': folder_name, 'path': path + '/' + folder_name if path else folder_name}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def edit_shared_file(self, filename: str, old_content: str, new_content: str) -> Dict[str, Any]:
        """修改共享工作区中的文件（查找替换方式）"""
        file_path = os.path.normpath(os.path.join(self.shared_workspace_dir, filename))
        if not file_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            new_file_content, match_mode = _replace_content_block(
                content, old_content, new_content
            )
            if new_file_content is None:
                return {'success': False, 'error': '未找到要替换的内容'}
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_file_content)

            return {
                'success': True,
                'filename': filename,
                'scope': 'shared',
                'size': len(new_file_content.encode('utf-8')),
                'match_mode': match_mode,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def delete_shared_file(self, filename: str) -> Dict[str, Any]:
        """删除共享工作区中的文件或文件夹"""
        file_path = os.path.normpath(os.path.join(self.shared_workspace_dir, filename))
        if not file_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        try:
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            else:
                os.remove(file_path)
            return {'success': True, 'filename': filename}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def move_shared_file(self, filename: str, target_path: str = '') -> Dict[str, Any]:
        """移动共享工作区中的文件"""
        src_path = os.path.normpath(os.path.join(self.shared_workspace_dir, filename))
        if not src_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(src_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}

        if target_path:
            dst_dir = os.path.normpath(os.path.join(self.shared_workspace_dir, target_path))
            # 检查目标路径是否在共享工作区内
            if not dst_dir.startswith(os.path.normpath(self.shared_workspace_dir)):
                return {'success': False, 'error': '目标路径不合法，不能超出共享工作区'}
        else:
            dst_dir = self.shared_workspace_dir

        if not os.path.exists(dst_dir) or not os.path.isdir(dst_dir):
            return {'success': False, 'error': '目标目录不存在'}

        try:
            shutil.move(src_path, dst_dir)
            new_path = os.path.join(target_path, os.path.basename(filename)) if target_path else os.path.basename(filename)
            return {'success': True, 'new_path': new_path.replace('\\', '/')}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def move_to_shared(self, session_id: str, filename: str, target_path: str = '') -> Dict[str, Any]:
        """将私有工作区的文件移动到共享工作区"""
        src_path = self.get_file_path(session_id, filename)
        if not src_path:
            return {'success': False, 'error': f'文件不存在: {filename}'}
        
        if not os.path.exists(src_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}
        
        if target_path:
            dst_dir = os.path.normpath(os.path.join(self.shared_workspace_dir, target_path))
            if not dst_dir.startswith(os.path.normpath(self.shared_workspace_dir)):
                return {'success': False, 'error': '目标路径不合法'}
        else:
            dst_dir = self.shared_workspace_dir
        
        if not os.path.exists(dst_dir):
            return {'success': False, 'error': '目标目录不存在'}
        
        try:
            filename_only = os.path.basename(filename)
            dst_path = os.path.join(dst_dir, filename_only)
            
            # 如果目标已存在，添加后缀
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(filename_only)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(dst_dir, f"{name}_{counter}{ext}")
                    counter += 1
            
            shutil.move(src_path, dst_path)
            new_path = os.path.join(target_path, os.path.basename(dst_path)) if target_path else os.path.basename(dst_path)
            return {
                'success': True, 
                'new_path': new_path.replace('\\', '/'),
                'scope': 'shared'
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def move_from_shared(self, session_id: str, filename: str, target_path: str = '') -> Dict[str, Any]:
        """将共享工作区的文件移动到私有工作区"""
        src_path = os.path.normpath(os.path.join(self.shared_workspace_dir, filename))
        if not src_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法'}
        
        if not os.path.exists(src_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}
        
        ws_path = self.get_or_create(session_id, 'web')
        
        if target_path:
            dst_dir = os.path.normpath(os.path.join(ws_path, target_path))
            if not dst_dir.startswith(os.path.normpath(ws_path)):
                return {'success': False, 'error': '目标路径不合法'}
        else:
            dst_dir = ws_path
        
        if not os.path.exists(dst_dir):
            return {'success': False, 'error': '目标目录不存在'}
        
        try:
            filename_only = os.path.basename(filename)
            dst_path = os.path.join(dst_dir, filename_only)
            
            # 如果目标已存在，添加后缀
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(filename_only)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(dst_dir, f"{name}_{counter}{ext}")
                    counter += 1
            
            shutil.move(src_path, dst_path)
            new_path = os.path.join(target_path, os.path.basename(dst_path)) if target_path else os.path.basename(dst_path)
            return {
                'success': True, 
                'new_path': new_path.replace('\\', '/'),
                'scope': 'private'
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _load_meta(self):
        """加载工作区元数据，并修复跨平台路径问题"""
        if os.path.exists(self.meta_file):
            try:
                with open(self.meta_file, 'r', encoding='utf-8') as f:
                    loaded_meta = json.load(f)

                # 修复跨平台路径问题
                # 如果元数据中的路径是其他系统的格式（如Windows路径在Linux上），需要重新计算
                self._meta = {}
                for session_id, info in loaded_meta.items():
                    old_path = info.get('path', '')
                    folder = info.get('folder', '')

                    # 重新计算当前平台下的正确路径
                    # 使用保存的folder名称，在当前workspaces_dir下重建路径
                    if folder:
                        new_path = os.path.join(self.workspaces_dir, folder)
                    else:
                        # 如果没有folder字段，从session_id生成
                        new_path = os.path.join(self.workspaces_dir, session_id)

                    # 规范化路径
                    new_path = os.path.normpath(new_path)

                    # 如果路径发生了变化，记录日志
                    if old_path != new_path:
                        _log.info(f"修复工作区路径: {old_path} -> {new_path}")

                    # 更新路径
                    info['path'] = new_path
                    self._meta[session_id] = info

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

        # 确保父目录存在（必须在工作区内）
        parent_dir = os.path.dirname(file_path)
        if not parent_dir.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法，不能超出工作区范围'}

        try:
            if parent_dir != ws_path:
                os.makedirs(parent_dir, exist_ok=True)
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

    def create_shared_file(self, filename: str, content: str) -> Dict[str, Any]:
        """
        在共享工作区中创建/覆盖文本文件

        Args:
            filename: 文件名
            content: 文件内容

        Returns:
            操作结果
        """
        safe_name = self._safe_filename(filename)

        # 支持子目录，但限制在共享工作区内
        file_path = os.path.normpath(os.path.join(self.shared_workspace_dir, safe_name))
        if not file_path.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法，不能超出共享工作区范围'}

        # 确保父目录存在（必须在共享工作区内）
        parent_dir = os.path.dirname(file_path)
        if not parent_dir.startswith(os.path.normpath(self.shared_workspace_dir)):
            return {'success': False, 'error': '路径不合法，不能超出共享工作区范围'}

        try:
            if parent_dir != self.shared_workspace_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return {
                'success': True,
                'filename': safe_name,
                'path': file_path,
                'scope': 'shared',
                'size': len(content.encode('utf-8'))
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def read_file(self, session_id: str, filename: str, 
                  start_line: int = None, end_line: int = None,
                  char_count: int = None, start_char: int = None) -> Dict[str, Any]:
        """
        读取工作区中的文件内容（供 AI 工具调用）

        Args:
            session_id: 会话ID
            filename: 文件名
            start_line: 开始行号（从1开始），与 char_count/start_char 二选一
            end_line: 结束行号（包含），需要与 start_line 配合使用
            char_count: 读取的字符数量（从文件开头或 start_char 指定的位置）
            start_char: 从第几个字符开始（从0开始），需要与 char_count 配合使用

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
                
                # 应用字符范围参数
                if char_count is not None:
                    start_pos = start_char if start_char is not None else 0
                    if start_pos < len(content):
                        content = content[start_pos:start_pos + char_count]
                    else:
                        content = ""
                
                # 应用行范围参数
                if start_line is not None or end_line is not None:
                    lines = content.split('\n')
                    start = max(0, (start_line - 1) if start_line else 0)
                    end = min(len(lines), end_line if end_line else len(lines))
                    if start < end:
                        content = '\n'.join(lines[start:end])
                        if end_line and end < len(lines):
                            content += '\n...'
                    else:
                        content = ""
                
                # 限制返回大小（如果没有指定具体范围）
                if not any([char_count, start_char, start_line, end_line]):
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

            new_file_content, match_mode = _replace_content_block(
                content, old_content, new_content
            )
            if new_file_content is None:
                return {'success': False, 'error': '未找到要替换的内容'}
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_file_content)

            return {
                'success': True,
                'filename': filename,
                'size': len(new_file_content.encode('utf-8')),
                'match_mode': match_mode,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def delete_file(self, session_id: str, filename: str) -> Dict[str, Any]:
        """
        删除工作区中的文件或文件夹（供 AI 工具调用）
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': False, 'error': '工作区不存在'}

        file_path = os.path.normpath(os.path.join(ws_path, filename))
        if not file_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法'}

        if not os.path.exists(file_path):
            return self.delete_file_reference(session_id, filename)

        try:
            if os.path.isdir(file_path):
                # 删除文件夹及其所有内容
                import shutil
                shutil.rmtree(file_path)
            else:
                os.remove(file_path)
            return {'success': True, 'filename': filename}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def list_files(self, session_id: str, path: str = '') -> Dict[str, Any]:
        """
        列出工作区中的文件
        
        Args:
            session_id: 会话ID
            path: 子目录路径（可选），为空则列出根目录
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': True, 'files': [], 'message': '工作区为空'}

        # 如果指定了路径，列出该目录的内容
        if path:
            target_path = os.path.join(ws_path, path)
            if not os.path.exists(target_path) or not os.path.isdir(target_path):
                return {'success': False, 'error': '目录不存在'}
            
            files = []
            try:
                for name in os.listdir(target_path):
                    full_path = os.path.join(target_path, name)
                    if os.path.isdir(full_path):
                        files.append({
                            'name': name,
                            'type': 'directory',
                            'size': 0,
                            'path': path + '/' + name if path else name
                        })
                    else:
                        mime_type, _ = mimetypes.guess_type(full_path)
                        files.append({
                            'name': name,
                            'type': 'file',
                            'size': os.path.getsize(full_path),
                            'mime_type': mime_type or 'application/octet-stream',
                            'path': path + '/' + name if path else name
                        })
            except Exception as e:
                return {'success': False, 'error': str(e)}
            
            return {'success': True, 'files': files, 'count': len(files)}

        # 列出根目录
        files = []
        try:
            for name in os.listdir(ws_path):
                full_path = os.path.join(ws_path, name)
                if os.path.isdir(full_path):
                    files.append({
                        'name': name,
                        'type': 'directory',
                        'size': 0,
                        'path': name
                    })
                else:
                    mime_type, _ = mimetypes.guess_type(full_path)
                    files.append({
                        'name': name,
                        'type': 'file',
                        'size': os.path.getsize(full_path),
                        'mime_type': mime_type or 'application/octet-stream',
                        'path': name
                    })
        except Exception as e:
            return {'success': False, 'error': str(e)}

        if not path:
            for ref in self._get_file_references(session_id):
                source_path = ref.get("source_path")
                if not source_path or not os.path.exists(source_path):
                    continue
                files.append({
                    'name': ref.get('name', os.path.basename(source_path)),
                    'type': 'file',
                    'size': ref.get('size', os.path.getsize(source_path)),
                    'mime_type': ref.get('mime_type', 'application/octet-stream'),
                    'path': ref.get('path', ref.get('name', '')),
                    'scope': 'private',
                    'reference': True,
                    'reference_kind': ref.get('reference_kind', 'external'),
                })

        return {'success': True, 'files': files, 'count': len(files)}

    def list_files_recursive(self, session_id: str, path: str = '') -> Dict[str, Any]:
        """递归列出工作区中所有子目录的文件"""
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': True, 'files': [], 'message': '工作区为空'}

        target_path = os.path.join(ws_path, path) if path else ws_path
        if not os.path.exists(target_path) or not os.path.isdir(target_path):
            return {'success': False, 'error': '目录不存在'}

        all_items = []
        
        def scan_directory(current_path: str, relative_prefix: str = ''):
            try:
                for name in os.listdir(current_path):
                    full_path = os.path.join(current_path, name)
                    relative_path = relative_prefix + '/' + name if relative_prefix else name
                    
                    if os.path.isdir(full_path):
                        # 先添加文件夹，然后递归扫描其内容
                        all_items.append({
                            'name': name,
                            'type': 'directory',
                            'size': 0,
                            'path': relative_path,
                            'scope': 'private'
                        })
                        scan_directory(full_path, relative_path)
                    else:
                        mime_type, _ = mimetypes.guess_type(full_path)
                        all_items.append({
                            'name': name,
                            'type': 'file',
                            'size': os.path.getsize(full_path),
                            'mime_type': mime_type or 'application/octet-stream',
                            'path': relative_path,
                            'scope': 'private'
                        })
            except Exception:
                pass
        
        scan_directory(target_path, path)
        
        return {
            'success': True,
            'files': all_items,
            'count': len(all_items),
            'scope': 'private',
            'recursive': True
        }

    def move_file(self, session_id: str, filename: str, target_path: str = '') -> Dict[str, Any]:
        """
        移动工作区中的文件或文件夹到指定目录
        
        Args:
            session_id: 会话ID
            filename: 要移动的文件或文件夹路径
            target_path: 目标目录路径（相对于工作区根目录）
        """
        ws_path = self.get_workspace(session_id)
        if not ws_path:
            return {'success': False, 'error': '工作区不存在'}
        
        src_path = os.path.normpath(os.path.join(ws_path, filename))
        if not src_path.startswith(os.path.normpath(ws_path)):
            return {'success': False, 'error': '路径不合法'}
        
        if not os.path.exists(src_path):
            return {'success': False, 'error': f'文件不存在: {filename}'}
        
        # 构建目标路径
        if target_path:
            dst_dir = os.path.join(ws_path, target_path)
        else:
            dst_dir = ws_path
        
        if not os.path.exists(dst_dir) or not os.path.isdir(dst_dir):
            return {'success': False, 'error': '目标目录不存在'}
        
        # 检查是否移动到自身目录下
        dst_path = os.path.join(dst_dir, os.path.basename(filename))
        if dst_path.startswith(src_path + os.sep) or dst_path == src_path:
            return {'success': False, 'error': '不能将文件夹移动到自身目录下'}
        
        try:
            import shutil
            shutil.move(src_path, dst_path)
            new_path = os.path.join(target_path, os.path.basename(filename)) if target_path else os.path.basename(filename)
            return {'success': True, 'new_path': new_path.replace('\\', '/')}
        except Exception as e:
            return {'success': False, 'error': str(e)}

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
        ref = self.get_file_reference(session_id, filename)
        if ref:
            return ref.get("source_path")
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

    def _get_file_references(self, session_id: str) -> List[Dict[str, Any]]:
        info = self._meta.get(session_id)
        if not info:
            return []
        refs = info.get("file_references")
        if isinstance(refs, list):
            return refs
        info["file_references"] = []
        return info["file_references"]

    def register_file_reference(
        self,
        session_id: str,
        source_path: str,
        display_name: str,
        *,
        session_type: str = "web",
        relative_path: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ws_path = self.get_or_create(session_id, session_type)
        if not ws_path:
            return {"success": False, "error": "Workspace not found"}

        normalized_source = os.path.normpath(source_path)
        if not os.path.exists(normalized_source):
            return {"success": False, "error": "Source file not found"}

        safe_name = self._safe_filename(display_name)
        safe_relative = self._safe_filename(relative_path) if relative_path else ""
        reference_path = f"{safe_relative}/{safe_name}" if safe_relative else safe_name

        refs = self._get_file_references(session_id)
        refs[:] = [ref for ref in refs if ref.get("path") != reference_path]

        mime_type, _ = mimetypes.guess_type(normalized_source)
        reference = {
            "name": safe_name,
            "path": reference_path,
            "source_path": normalized_source,
            "size": os.path.getsize(normalized_source),
            "mime_type": mime_type or "application/octet-stream",
            "reference": True,
            "reference_kind": "external",
            "created_at": datetime.now().isoformat(),
        }
        if metadata:
            reference["metadata"] = metadata

        refs.append(reference)
        self._save_meta()
        return {"success": True, "reference": reference}

    def get_file_reference(
        self, session_id: str, filename: str
    ) -> Optional[Dict[str, Any]]:
        normalized_name = filename.replace("\\", "/")
        for ref in self._get_file_references(session_id):
            if ref.get("path") == normalized_name:
                source_path = ref.get("source_path")
                if source_path and os.path.exists(source_path):
                    return ref
        return None

    def delete_file_reference(self, session_id: str, filename: str) -> Dict[str, Any]:
        refs = self._get_file_references(session_id)
        normalized_name = filename.replace("\\", "/")
        original_count = len(refs)
        refs[:] = [ref for ref in refs if ref.get("path") != normalized_name]
        if len(refs) == original_count:
            return {"success": False, "error": "File not found"}
        self._save_meta()
        return {"success": True, "filename": normalized_name, "reference": True}

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
