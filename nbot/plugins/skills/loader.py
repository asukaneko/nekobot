# 外部技能加载器
import os
import importlib.util
import logging
from pathlib import Path
from typing import List, Optional

_log = logging.getLogger(__name__)


class SkillLoader:
    """外部技能加载器"""

    def __init__(self, external_dir: str = "nbot/plugins/skills/external"):
        self.external_dir = Path(external_dir)
        self.loaded_modules = {}

    def discover_skills(self) -> List[str]:
        """发现外部技能文件"""
        if not self.external_dir.exists():
            _log.warning(f"External skills directory not found: {self.external_dir}")
            return []

        skill_files = []
        for file in self.external_dir.glob("*.py"):
            if file.stem != "__init__":
                skill_files.append(str(file))
        return skill_files

    def load_skill(self, file_path: str) -> bool:
        """加载单个技能文件"""
        try:
            module_name = Path(file_path).stem

            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                _log.error(f"Failed to create spec for {file_path}")
                return False

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if hasattr(module, "register_skill"):
                module.register_skill()
                _log.info(f"Loaded external skill from {file_path}")
                self.loaded_modules[module_name] = module
                return True
            else:
                _log.warning(f"No register_skill function found in {file_path}")
                return False

        except Exception as e:
            _log.error(f"Failed to load skill from {file_path}: {e}")
            return False

    def load_all(self) -> int:
        """加载所有外部技能"""
        skill_files = self.discover_skills()
        loaded_count = 0

        for file_path in skill_files:
            if self.load_skill(file_path):
                loaded_count += 1

        _log.info(f"Loaded {loaded_count} external skills")
        return loaded_count

    def reload_skill(self, module_name: str) -> bool:
        """重新加载指定技能"""
        if module_name in self.loaded_modules:
            del self.loaded_modules[module_name]

        file_path = self.external_dir / f"{module_name}.py"
        if file_path.exists():
            return self.load_skill(str(file_path))
        return False

    def unload_skill(self, module_name: str):
        """卸载指定技能"""
        if module_name in self.loaded_modules:
            del self.loaded_modules[module_name]
            _log.info(f"Unloaded skill: {module_name}")
