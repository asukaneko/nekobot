"""
Skills 存储管理器
按照 AI Agent 开发领域的新兴标准打包技能

目录结构：
skill-name/
├── SKILL.md              # 技能核心定义（必填）
├── reference.md          # 详细参考资料（可选）
├── LICENSE.txt           # 许可证信息（可选）
├── resources/           # 附加资源（可选）
│   ├── template.xlsx    # 示例文件
│   └── data.json        # 数据文件
└── scripts/             # 可执行脚本（可选）
    ├── main.py          # 主实现
    └── helper.py       # 辅助函数
"""
import os
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

_log = logging.getLogger(__name__)

# Skills 存储根目录 - 使用绝对路径
_current_dir = os.path.dirname(os.path.abspath(__file__))
SKILLS_ROOT = os.path.abspath(os.path.join(_current_dir, '..', '..', 'data', 'skills'))

# 确保根目录存在
os.makedirs(SKILLS_ROOT, exist_ok=True)


class SkillStorage:
    """Skill 存储类 - 按照新的文件夹结构"""

    def __init__(self, skill_name: str):
        self.skill_name = skill_name
        self.skill_dir = os.path.join(SKILLS_ROOT, skill_name)
        self.scripts_dir = os.path.join(self.skill_dir, 'scripts')
        self.resources_dir = os.path.join(self.skill_dir, 'resources')

        # 核心文件路径
        self.skill_md_file = os.path.join(self.skill_dir, 'SKILL.md')
        self.reference_md_file = os.path.join(self.skill_dir, 'reference.md')
        self.license_file = os.path.join(self.skill_dir, 'LICENSE.txt')

        # 确保必要的目录存在
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保必要的目录存在"""
        os.makedirs(self.scripts_dir, exist_ok=True)
        os.makedirs(self.resources_dir, exist_ok=True)

    def save_skill_md(self, content: str = ""):
        """保存 SKILL.md - 技能核心定义"""
        if not content:
            content = self._get_default_skill_md()
        with open(self.skill_md_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return self.skill_md_file

    def load_skill_md(self) -> Optional[str]:
        """加载 SKILL.md"""
        if os.path.exists(self.skill_md_file):
            try:
                with open(self.skill_md_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                _log.error(f"Failed to load SKILL.md for {self.skill_name}: {e}")
        return None

    def save_reference_md(self, content: str = ""):
        """保存 reference.md - 详细参考资料"""
        with open(self.reference_md_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return self.reference_md_file

    def load_reference_md(self) -> Optional[str]:
        """加载 reference.md"""
        if os.path.exists(self.reference_md_file):
            try:
                with open(self.reference_md_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                _log.error(f"Failed to load reference.md for {self.skill_name}: {e}")
        return None

    def save_license(self, content: str = ""):
        """保存 LICENSE.txt"""
        with open(self.license_file, 'w', encoding='utf-8') as f:
            f.write(content)
        return self.license_file

    def load_license(self) -> Optional[str]:
        """加载 LICENSE.txt"""
        if os.path.exists(self.license_file):
            try:
                with open(self.license_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                _log.error(f"Failed to load LICENSE.txt for {self.skill_name}: {e}")
        return None

    def load_script(self, script_name: str) -> Optional[str]:
        """加载脚本文件"""
        # script_name 可能是完整路径（如 scripts/main.py）或纯文件名（如 main.py）
        if os.path.sep in script_name or script_name.startswith('scripts/'):
            # 完整路径，直接拼接 skill_dir
            script_path = os.path.join(self.skill_dir, script_name)
        else:
            # 纯文件名，拼接 scripts_dir
            script_path = os.path.join(self.scripts_dir, script_name)

        if os.path.exists(script_path):
            try:
                with open(script_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                _log.error(f"Failed to load script {script_name} for {self.skill_name}: {e}")
        return None

    def save_script(self, script_name: str, content: str):
        """保存脚本文件到 scripts/ 目录"""
        # script_name 可能是完整路径（如 scripts/main.py）或纯文件名（如 main.py）
        if os.path.sep in script_name or script_name.startswith('scripts/'):
            # 完整路径，直接拼接 skill_dir
            script_path = os.path.join(self.skill_dir, script_name)
        else:
            # 纯文件名，拼接 scripts_dir
            script_path = os.path.join(self.scripts_dir, script_name)

        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return script_path

    def list_scripts(self) -> List[str]:
        """列出所有脚本文件"""
        if not os.path.exists(self.scripts_dir):
            return []
        return [f for f in os.listdir(self.scripts_dir) if os.path.isfile(os.path.join(self.scripts_dir, f))]

    def delete_script(self, script_name: str) -> bool:
        """删除脚本文件"""
        script_path = os.path.join(self.scripts_dir, script_name)
        if os.path.exists(script_path):
            os.remove(script_path)
            return True
        return False

    def save_resource(self, resource_name: str, content: str):
        """保存资源文件到 resources/ 目录"""
        resource_path = os.path.join(self.resources_dir, resource_name)
        with open(resource_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return resource_path

    def load_resource(self, resource_name: str) -> Optional[str]:
        """加载资源文件"""
        resource_path = os.path.join(self.resources_dir, resource_name)
        if os.path.exists(resource_path):
            try:
                with open(resource_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                _log.error(f"Failed to load resource {resource_name} for {self.skill_name}: {e}")
        return None

    def list_resources(self) -> List[str]:
        """列出所有资源文件"""
        if not os.path.exists(self.resources_dir):
            return []
        return [f for f in os.listdir(self.resources_dir) if os.path.isfile(os.path.join(self.resources_dir, f))]

    def delete_resource(self, resource_name: str) -> bool:
        """删除资源文件"""
        resource_path = os.path.join(self.resources_dir, resource_name)
        if os.path.exists(resource_path):
            os.remove(resource_path)
            return True
        return False

    def get_all_files(self) -> List[Dict[str, Any]]:
        """获取所有文件的列表"""
        files = []
        if os.path.exists(self.skill_dir):
            for root, dirs, filenames in os.walk(self.skill_dir):
                for filename in filenames:
                    # 跳过 __pycache__ 目录
                    if '__pycache__' in root:
                        continue
                    filepath = os.path.join(root, filename)
                    rel_path = os.path.relpath(filepath, self.skill_dir)
                    stat = os.stat(filepath)
                    files.append({
                        'name': filename,
                        'path': rel_path,
                        'size': stat.st_size,
                        'modified': stat.st_mtime,
                        'type': self._get_file_type(rel_path)
                    })
        return files

    def _get_file_type(self, path: str) -> str:
        """获取文件类型"""
        # 统一使用正斜杠
        path = path.replace('\\', '/')

        if path == 'SKILL.md':
            return 'core'
        elif path == 'reference.md':
            return 'reference'
        elif path == 'LICENSE.txt':
            return 'license'
        elif path.startswith('scripts/'):
            return 'script'
        elif path.startswith('resources/'):
            return 'resource'
        else:
            return 'other'

    def _get_default_skill_md(self) -> str:
        """获取默认的 SKILL.md 模板"""
        return f"""# {self.skill_name}

## 基本信息

- **名称**: {self.skill_name}
- **版本**: 1.0.0
- **创建时间**: {self._get_current_time()}

## 功能描述

请描述这个技能的主要功能...

## 使用说明

1. 第一步...
2. 第二步...
3. 第三步...

## 输入参数

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| param1 | string | 是 | 参数1说明 |
| param2 | number | 否 | 参数2说明 |

## 输出格式

描述技能的输出格式...

## 使用示例

```python
# 示例代码
result = execute_skill("{self.skill_name}", {{"param1": "value"}})
print(result)
```

## 参考资料

详细的技术文档请参阅 [reference.md](reference.md)
"""

    def _get_current_time(self) -> str:
        """获取当前时间"""
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def save_config(self, config: Dict[str, Any]):
        """保存配置到 SKILL.md 中（兼容旧版本）"""
        content = self.load_skill_md() or self._get_default_skill_md()

        # 如果没有自定义内容，使用默认模板
        if not content.strip():
            content = self._get_default_skill_md()

        # 简单保存为 JSON（未来可以解析 Markdown）
        config_file = os.path.join(self.skill_dir, 'config.json')
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def load_config(self) -> Dict[str, Any]:
        """加载配置"""
        config_file = os.path.join(self.skill_dir, 'config.json')
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                _log.error(f"Failed to load config for {self.skill_name}: {e}")
        return {}

    def save_data(self, data: Dict[str, Any]):
        """保存技能运行时数据"""
        data_file = os.path.join(self.skill_dir, 'data.json')
        with open(data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def load_data(self) -> Dict[str, Any]:
        """加载技能运行时数据"""
        data_file = os.path.join(self.skill_dir, 'data.json')
        if os.path.exists(data_file):
            try:
                with open(data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                _log.error(f"Failed to load data for {self.skill_name}: {e}")
        return {}


class SkillsStorageManager:
    """Skills 存储管理器"""

    def __init__(self):
        self._ensure_root_dir()

    def _ensure_root_dir(self):
        """确保根目录存在"""
        os.makedirs(SKILLS_ROOT, exist_ok=True)

    def get_skill_storage(self, skill_name: str) -> SkillStorage:
        """获取指定 Skill 的存储"""
        return SkillStorage(skill_name)

    def list_skills(self) -> List[Dict[str, Any]]:
        """列出所有有存储的 Skills"""
        skills = []
        if os.path.exists(SKILLS_ROOT):
            for skill_name in os.listdir(SKILLS_ROOT):
                skill_dir = os.path.join(SKILLS_ROOT, skill_name)
                if os.path.isdir(skill_dir):
                    storage = SkillStorage(skill_name)
                    skill_md = storage.load_skill_md()
                    scripts = storage.list_scripts()
                    resources = storage.list_resources()
                    files = storage.get_all_files()

                    skills.append({
                        'name': skill_name,
                        'has_skill_md': os.path.exists(storage.skill_md_file),
                        'has_reference': os.path.exists(storage.reference_md_file),
                        'has_license': os.path.exists(storage.license_file),
                        'scripts': scripts,
                        'resources': resources,
                        'files': files,
                        'files_count': len(files),
                        'preview': skill_md[:200] if skill_md else None
                    })
        return skills

    def create_skill(self, skill_name: str, config: Dict[str, Any] = None) -> SkillStorage:
        """创建新的 Skill 存储"""
        storage = SkillStorage(skill_name)

        # 创建默认的 SKILL.md
        if not os.path.exists(storage.skill_md_file):
            storage.save_skill_md()

        # 保存配置（如果提供）
        if config:
            storage.save_config(config)

        return storage

    def delete_skill(self, skill_name: str) -> bool:
        """删除 Skill 存储"""
        import shutil
        skill_dir = os.path.join(SKILLS_ROOT, skill_name)
        if os.path.exists(skill_dir):
            shutil.rmtree(skill_dir)
            return True
        return False

    def skill_exists(self, skill_name: str) -> bool:
        """检查 Skill 是否存在"""
        skill_dir = os.path.join(SKILLS_ROOT, skill_name)
        return os.path.exists(skill_dir)

    def get_skill_info(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """获取 Skill 的详细信息"""
        if not self.skill_exists(skill_name):
            return None

        storage = self.get_skill_storage(skill_name)
        return {
            'name': skill_name,
            'skill_md': storage.load_skill_md(),
            'reference_md': storage.load_reference_md(),
            'license': storage.load_license(),
            'scripts': storage.list_scripts(),
            'resources': storage.list_resources(),
            'files': storage.get_all_files(),
            'config': storage.load_config(),
            'data': storage.load_data()
        }


# 全局实例
_skills_storage_manager = None


def get_skills_storage_manager() -> SkillsStorageManager:
    """获取 Skills 存储管理器"""
    global _skills_storage_manager
    if _skills_storage_manager is None:
        _skills_storage_manager = SkillsStorageManager()
    return _skills_storage_manager
