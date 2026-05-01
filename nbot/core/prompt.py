"""
提示词管理模块

提供统一的提示词加载和管理功能，支持：
- 加载用户/群组/默认提示词
- 加载记忆并添加到提示词
- 添加工具列表到提示词
- 支持自定义提示词模板
"""

import os
import json
from typing import Dict, List
from datetime import datetime


class PromptManager:
    """提示词管理器"""
    
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
        self.prompts_dir = os.path.join(self.base_dir, 'resources', 'prompts')
        self.default_prompt_file = os.path.join(self.prompts_dir, 'neko.txt')
        
        self._prompt_cache: Dict[str, str] = {}
        self._memories_cache: List[Dict] = []
        self._load_memories()
    
    def _load_memories(self):
        """加载记忆数据"""
        memories_file = os.path.join(self.base_dir, 'data', 'memories.json')
        if os.path.exists(memories_file):
            try:
                with open(memories_file, 'r', encoding='utf-8') as f:
                    self._memories_cache = json.load(f)
                # 迁移旧格式数据到新格式
                self._migrate_memories_format()
            except Exception as e:
                print(f"加载记忆文件失败: {e}")
                self._memories_cache = []
        else:
            self._memories_cache = []
    
    def _migrate_memories_format(self):
        """迁移旧格式记忆到新格式"""
        migrated = False
        for mem in self._memories_cache:
            # 检查是否是旧格式（使用 key 和 value）
            if 'key' in mem or 'value' in mem:
                # 迁移到新格式（使用 title, summary, content）
                mem['title'] = mem.pop('key', mem.get('title', ''))
                mem['content'] = mem.pop('value', mem.get('content', ''))
                if 'summary' not in mem:
                    # 从 content 提取前 100 字作为摘要
                    content = mem.get('content', '')
                    mem['summary'] = content[:100] + '...' if len(content) > 100 else content
                migrated = True
        
        if migrated:
            self._save_memories()
            print("记忆数据已迁移到新格式")

    def _save_memories(self):
        """保存记忆数据"""
        memories_file = os.path.join(self.base_dir, 'data', 'memories.json')
        os.makedirs(os.path.dirname(memories_file), exist_ok=True)
        try:
            with open(memories_file, 'w', encoding='utf-8') as f:
                json.dump(self._memories_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存记忆文件失败: {e}")
    
    def load_base_prompt(self, user_id: str = None, group_id: str = None) -> str:
        """加载基础提示词
        
        Args:
            user_id: 用户ID，如果指定则加载用户专属提示词
            group_id: 群组ID，如果指定则加载群组专属提示词
            
        Returns:
            提示词内容
        """
        prompt_file = None
        
        if user_id:
            prompt_file = os.path.join(self.prompts_dir, 'user', f'user_{user_id}.txt')
        elif group_id:
            prompt_file = os.path.join(self.prompts_dir, 'group', f'group_{group_id}.txt')
        
        if prompt_file and os.path.exists(prompt_file):
            try:
                with open(prompt_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"加载提示词失败: {e}")
        
        # 加载默认提示词
        if os.path.exists(self.default_prompt_file):
            try:
                with open(self.default_prompt_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"加载默认提示词失败: {e}")
        
        return ""
    
    def load_memories(self, user_id: str = None, group_id: str = None) -> str:
        """加载相关记忆
        
        Args:
            user_id: 用户ID
            group_id: 群组ID
            
        Returns:
            记忆内容字符串（包含标题和摘要）
        """
        self._load_memories()
        
        target_id = user_id or group_id
        if not target_id:
            return ""
        
        memories = []
        now = datetime.now()
        
        for mem in self._memories_cache:
            mem_target = mem.get('target_id', '')
            if mem_target and mem_target != target_id:
                continue
            
            mem_type = mem.get('type', 'long')
            
            # 检查是否过期
            if mem_type == 'short':
                created_at = mem.get('created_at', '')
                expire_days = mem.get('expire_days', 7)
                
                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at)
                        diff_days = (now - created).days
                        if diff_days > expire_days:
                            continue
                    except:
                        pass
            
            # 新格式：包含标题、摘要、内容
            title = mem.get('title', '')
            summary = mem.get('summary', '')
            content = mem.get('content', '')
            
            if title or content:
                # 优先使用摘要，如果没有摘要则使用内容的前100字
                display_text = summary if summary else (content[:100] + '...' if len(content) > 100 else content)
                memories.append(f"【{title}】{display_text}")
        
        if memories:
            return "\n".join(["【重要记忆】"] + memories)
        return ""
    
    def load_tools_prompt(self) -> str:
        """加载可用工具列表
        
        Returns:
            工具描述字符串
        """
        try:
            from nbot.services.tools import get_enabled_tools
            enabled_tools = get_enabled_tools()
            
            tools_text = "## 可用工具 (Tools)\n"
            tools_text += "你可以使用以下工具来帮助用户：\n\n"
            
            if enabled_tools:
                for tool in enabled_tools:
                    if tool.get("type") == "function" and "function" in tool:
                        func = tool["function"]
                        name = func.get("name", "")
                        desc = func.get("description", "")
                        if name and desc:
                            tools_text += f"- **{name}**: {desc}\n"
            
            # 添加工作区工具
            tools_text += "\n### 工作区工具 (Workspace)\n"
            tools_text += "每个会话都有独立的工作区，你可以使用以下工具操作工作区中的文件：\n"
            tools_text += "- **workspace_create_file**: 在工作区中创建或覆盖文件，适用于生成代码、文档等\n"
            tools_text += "- **workspace_read_file**: 读取工作区中的文件内容\n"
            tools_text += "- **workspace_edit_file**: 修改工作区中的文件（查找替换方式）\n"
            tools_text += "- **workspace_delete_file**: 删除工作区中的文件\n"
            tools_text += "- **workspace_list_files**: 列出工作区中的所有文件\n"
            tools_text += "- **workspace_send_file**: 将工作区中的文件发送给用户下载\n"
            
            tools_text += "\n### 命令执行工具 (Exec)\n"
            tools_text += "- **exec_command**: 执行命令行命令。白名单命令（如ls, cat, echo等）会直接执行，非白名单命令系统会自动向用户请求确认，用户确认后命令自动执行，你无需再次调用。\n"
            
            tools_text += "\n**使用规则：**\n"
            tools_text += "1. 当用户请求需要使用工具时，你可以调用对应的工具\n"
            tools_text += "2. 工具调用会被系统自动处理\n"
            tools_text += "3. 用户上传的文件会自动保存到工作区，你可以使用 workspace_read_file 查看\n"
            tools_text += "4. 使用 exec_command 时，如果命令不在白名单中，系统会自动请求用户确认。用户确认后系统会直接执行命令，你无需再次调用 exec_command\n"
            
            return tools_text
        except ImportError:
            return ""
        except Exception as e:
            print(f"加载工具列表失败: {e}")
            return ""
    
    def load_prompt(self, user_id: str = None, group_id: str = None, 
                   include_memories: bool = True, include_tools: bool = True) -> str:
        """加载完整提示词（基础提示词 + 记忆 + 工具）
        
        Args:
            user_id: 用户ID
            group_id: 群组ID
            include_memories: 是否包含记忆
            include_tools: 是否包含工具列表
            
        Returns:
            完整的提示词
        """
        parts = []
        
        base_prompt = self.load_base_prompt(user_id, group_id)
        if base_prompt:
            parts.append(base_prompt)
        
        if include_memories:
            memories_text = self.load_memories(user_id, group_id)
            if memories_text:
                parts.append(memories_text)
        
        if include_tools:
            tools_text = self.load_tools_prompt()
            if tools_text:
                parts.append(tools_text)
        
        return "\n\n".join(parts)
    
    def save_prompt(self, content: str, user_id: str = None, group_id: str = None) -> bool:
        """保存提示词到文件
        
        Args:
            content: 提示词内容
            user_id: 用户ID
            group_id: 群组ID
            
        Returns:
            是否保存成功
        """
        prompt_file = None
        
        if user_id:
            prompt_dir = os.path.join(self.prompts_dir, 'user')
            prompt_file = os.path.join(prompt_dir, f'user_{user_id}.txt')
        elif group_id:
            prompt_dir = os.path.join(self.prompts_dir, 'group')
            prompt_file = os.path.join(prompt_dir, f'group_{group_id}.txt')
        else:
            prompt_file = self.default_prompt_file
        
        try:
            os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
            with open(prompt_file, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"保存提示词失败: {e}")
            return False
    
    def add_memory(self, title: str, content: str, target_id: str, 
                   summary: str = None, mem_type: str = 'long', 
                   expire_days: int = 7) -> bool:
        """添加记忆
        
        Args:
            title: 记忆标题
            content: 记忆内容
            target_id: 目标ID（用户或群组）
            summary: 记忆摘要（可选，默认从 content 提取）
            mem_type: 记忆类型 ('long' 长期, 'short' 短期)
            expire_days: 短期记忆过期天数
            
        Returns:
            是否添加成功
        """
        # 如果没有提供摘要，从内容中提取
        if not summary:
            summary = content[:100] + '...' if len(content) > 100 else content
        
        memory = {
            'id': f"mem_{datetime.now().timestamp()}",
            'title': title,
            'summary': summary,
            'content': content,
            'target_id': target_id,
            'type': mem_type,
            'expire_days': expire_days,
            'created_at': datetime.now().isoformat()
        }
        
        self._memories_cache.append(memory)
        self._save_memories()
        return True
    
    def get_memories(self, target_id: str = None, mem_type: str = None) -> List[Dict]:
        """获取记忆列表
        
        Args:
            target_id: 目标ID（可选）
            mem_type: 记忆类型（可选）
            
        Returns:
            记忆列表
        """
        self._load_memories()
        
        result = self._memories_cache
        
        if target_id:
            result = [m for m in result if m.get('target_id') == target_id]
        
        if mem_type:
            result = [m for m in result if m.get('type') == mem_type]
        
        return result
    
    def delete_memory(self, memory_id: str) -> bool:
        """删除记忆
        
        Args:
            memory_id: 记忆ID
            
        Returns:
            是否删除成功
        """
        self._memories_cache = [m for m in self._memories_cache if m.get('id') != memory_id]
        self._save_memories()
        return True
    
    def clear_memories(self, target_id: str = None) -> bool:
        """清空记忆
        
        Args:
            target_id: 目标ID，如果指定则只清空该目标的记忆
            
        Returns:
            是否清空成功
        """
        if target_id:
            self._memories_cache = [m for m in self._memories_cache if m.get('target_id') != target_id]
        else:
            self._memories_cache = []
        
        self._save_memories()
        return True


# 创建全局实例
prompt_manager = PromptManager()


def load_prompt(user_id: str = None, group_id: str = None,
                include_memories: bool = True, include_tools: bool = True) -> str:
    """便捷函数：加载提示词
    
    Args:
        user_id: 用户ID
        group_id: 群组ID
        include_memories: 是否包含记忆
        include_tools: 是否包含工具列表
        
    Returns:
        完整提示词
    """
    return prompt_manager.load_prompt(user_id, group_id, include_memories, include_tools)


def load_memories(user_id: str = None, group_id: str = None) -> str:
    """便捷函数：加载记忆
    
    Args:
        user_id: 用户ID
        group_id: 群组ID
        
    Returns:
        记忆内容
    """
    return prompt_manager.load_memories(user_id, group_id)


def add_memory(title: str, content: str, target_id: str,
              summary: str = None, mem_type: str = 'long', 
              expire_days: int = 7) -> bool:
    """便捷函数：添加记忆
    
    Args:
        title: 记忆标题
        content: 记忆内容
        target_id: 目标ID
        summary: 记忆摘要
        mem_type: 记忆类型
        expire_days: 过期天数
        
    Returns:
        是否添加成功
    """
    return prompt_manager.add_memory(title, content, target_id, summary, mem_type, expire_days)


def save_prompt(content: str, user_id: str = None, group_id: str = None) -> bool:
    """便捷函数：保存提示词
    
    Args:
        content: 提示词内容
        user_id: 用户ID
        group_id: 群组ID
        
    Returns:
        是否保存成功
    """
    return prompt_manager.save_prompt(content, user_id, group_id)


def get_memories(target_id: str = None, mem_type: str = None) -> List[Dict]:
    """便捷函数：获取记忆列表
    
    Args:
        target_id: 目标ID
        mem_type: 记忆类型
        
    Returns:
        记忆列表
    """
    return prompt_manager.get_memories(target_id, mem_type)


def delete_memory(memory_id: str) -> bool:
    """便捷函数：删除记忆
    
    Args:
        memory_id: 记忆ID
        
    Returns:
        是否删除成功
    """
    return prompt_manager.delete_memory(memory_id)


def clear_memories(target_id: str = None) -> bool:
    """便捷函数：清空记忆
    
    Args:
        target_id: 目标ID
        
    Returns:
        是否清空成功
    """
    return prompt_manager.clear_memories(target_id)
