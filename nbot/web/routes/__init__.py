from nbot.web.routes.achievements import register_achievement_routes
from nbot.web.routes.admin_misc import register_admin_misc_routes
from nbot.web.routes.ai_config import register_ai_config_routes
from nbot.web.routes.ai_models import register_ai_model_routes
from nbot.web.routes.api_keys import register_api_key_routes
from nbot.web.routes.auth import register_auth_routes
from nbot.web.routes.channels import register_channel_routes
from nbot.web.routes.characters import register_character_routes
from nbot.web.routes.config_legacy import register_config_legacy_routes
from nbot.web.routes.config_transfer import register_config_transfer_routes
from nbot.web.routes.export_center import register_export_center_routes
from nbot.web.routes.files import register_file_routes
from nbot.web.routes.gateway import register_gateway_routes
from nbot.web.routes.gateway_logs import register_gateway_log_routes
from nbot.web.routes.global_search import register_global_search_routes
from nbot.web.routes.groups import register_group_routes
from nbot.web.routes.heartbeat import register_heartbeat_routes
from nbot.web.routes.hooks import register_hook_routes
from nbot.web.routes.images import register_image_routes
from nbot.web.routes.knowledge import register_knowledge_routes
from nbot.web.routes.live2d import register_live2d_routes
from nbot.web.routes.login_tokens import register_login_token_routes
from nbot.web.routes.mcp_servers import register_mcp_server_routes
from nbot.web.routes.memory import register_memory_routes
from nbot.web.routes.message_filter import register_message_filter_routes
from nbot.web.routes.oauth import register_oauth_routes
from nbot.web.routes.personality import register_personality_routes
from nbot.web.routes.personas import register_persona_routes
from nbot.web.routes.plot import register_plot_routes
from nbot.web.routes.plugins import register_plugin_routes
from nbot.web.routes.prompt_manager import register_prompt_preset_routes
from nbot.web.routes.public_sessions import register_public_session_routes
from nbot.web.routes.push import register_push_routes
from nbot.web.routes.qq_overview import register_qq_overview_routes
from nbot.web.routes.qrcode import register_qrcode_routes
from nbot.web.routes.review import register_review_routes
from nbot.web.routes.sessions import register_session_routes
from nbot.web.routes.skills import register_skill_routes
from nbot.web.routes.skills_storage import register_skills_storage_routes
from nbot.web.routes.smart_routing import register_smart_routing_routes
from nbot.web.routes.ssl_validation import register_ssl_validation_routes
from nbot.web.routes.stickers import register_sticker_routes
from nbot.web.routes.task_center import register_task_center_routes
from nbot.web.routes.tools import register_tool_routes
from nbot.web.routes.tts_playground import register_tts_playground_routes
from nbot.web.routes.update import register_update_routes
from nbot.web.routes.voice import register_voice_routes
from nbot.web.routes.web_agent import register_web_agent_routes
from nbot.web.routes.webdav_backup import register_webdav_backup_routes
from nbot.web.routes.workflows import register_workflow_routes
from nbot.web.routes.workspace_misc import register_workspace_misc_routes
from nbot.web.routes.workspace_private import register_workspace_private_routes
from nbot.web.routes.workspace_shared import register_workspace_shared_routes
from nbot.web.routes.world_book import register_world_book_routes
from nbot.web.routes.worlds import register_world_routes

__all__ = [
    "register_admin_misc_routes",
    "register_ai_config_routes",
    "register_ai_model_routes",
    "register_api_key_routes",
    "register_auth_routes",
    "register_channel_routes",
    "register_character_routes",
    "register_config_legacy_routes",
    "register_config_transfer_routes",
    "register_export_center_routes",
    "register_file_routes",
    "register_gateway_routes",
    "register_group_routes",
    "register_gateway_log_routes",
    "register_global_search_routes",
    "register_heartbeat_routes",
    "register_hook_routes",
    "register_image_routes",
    "register_knowledge_routes",
    "register_live2d_routes",
    "register_login_token_routes",
    "register_mcp_server_routes",
    "register_memory_routes",
    "register_message_filter_routes",
    "register_personality_routes",
    "register_persona_routes",
    "register_prompt_preset_routes",
    "register_plugin_routes",
    "register_public_session_routes",
    "register_plot_routes",
    "register_push_routes",
    "register_qq_overview_routes",
    "register_qrcode_routes",
    "register_review_routes",
    "register_session_routes",
    "register_skill_routes",
    "register_skills_storage_routes",
    "register_ssl_validation_routes",
    "register_task_center_routes",
    "register_tool_routes",
    "register_tts_playground_routes",
    "register_update_routes",
    "register_voice_routes",
    "register_web_agent_routes",
    "register_webdav_backup_routes",
    "register_workflow_routes",
    "register_world_book_routes",
    "register_world_routes",
    "register_workspace_private_routes",
    "register_workspace_shared_routes",
    "register_workspace_misc_routes",
    "register_oauth_routes",
    "register_achievement_routes",
    "register_smart_routing_routes",
    "register_sticker_routes",
]
