from nbot.web.routes.admin_misc import register_admin_misc_routes
from nbot.web.routes.ai_config import register_ai_config_routes
from nbot.web.routes.ai_models import register_ai_model_routes
from nbot.web.routes.api_keys import register_api_key_routes
from nbot.web.routes.auth import register_auth_routes
from nbot.web.routes.channels import register_channel_routes
from nbot.web.routes.config_legacy import register_config_legacy_routes
from nbot.web.routes.files import register_file_routes
from nbot.web.routes.heartbeat import register_heartbeat_routes
from nbot.web.routes.knowledge import register_knowledge_routes
from nbot.web.routes.live2d import register_live2d_routes
from nbot.web.routes.memory import register_memory_routes
from nbot.web.routes.personality import register_personality_routes
from nbot.web.routes.push import register_push_routes
from nbot.web.routes.qq_overview import register_qq_overview_routes
from nbot.web.routes.sessions import register_session_routes
from nbot.web.routes.skills import register_skill_routes
from nbot.web.routes.skills_storage import register_skills_storage_routes
from nbot.web.routes.task_center import register_task_center_routes
from nbot.web.routes.tools import register_tool_routes
from nbot.web.routes.voice import register_voice_routes
from nbot.web.routes.web_agent import register_web_agent_routes
from nbot.web.routes.workflows import register_workflow_routes
from nbot.web.routes.workspace_private import register_workspace_private_routes
from nbot.web.routes.workspace_shared import register_workspace_shared_routes
from nbot.web.routes.workspace_misc import register_workspace_misc_routes

__all__ = [
    "register_admin_misc_routes",
    "register_ai_config_routes",
    "register_ai_model_routes",
    "register_api_key_routes",
    "register_auth_routes",
    "register_channel_routes",
    "register_config_legacy_routes",
    "register_file_routes",
    "register_heartbeat_routes",
    "register_knowledge_routes",
    "register_live2d_routes",
    "register_memory_routes",
    "register_personality_routes",
    "register_push_routes",
    "register_qq_overview_routes",
    "register_session_routes",
    "register_skill_routes",
    "register_skills_storage_routes",
    "register_task_center_routes",
    "register_tool_routes",
    "register_voice_routes",
    "register_web_agent_routes",
    "register_workflow_routes",
    "register_workspace_private_routes",
    "register_workspace_shared_routes",
    "register_workspace_misc_routes",
]
