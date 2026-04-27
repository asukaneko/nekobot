# core module

from nbot.core.prompt import (
    PromptManager,
    prompt_manager,
    load_prompt,
    load_memories,
    add_memory,
    save_prompt,
    get_memories,
    delete_memory,
    clear_memories,
)

from nbot.core.message import (
    Message,
    MessageManager,
    message_manager,
    add_message,
    get_messages,
    create_message,
    migrate_messages,
)

from nbot.core.workspace import (
    WorkspaceManager,
    workspace_manager,
)

from nbot.core.agent_service import (
    AgentService,
    ToolLoopExit,
    ToolLoopHooks,
    ToolLoopSession,
    PreparedChatContext,
    ToolExecutionResult,
    ToolLoopResult,
    apply_tool_call_history,
    build_continue_chat_response,
    clean_response_content,
    execute_tool_loop_session,
    expand_hidden_tool_history,
    run_tool_loop_session,
    extract_display_text,
    extract_tool_call_history,
    normalize_chat_response,
    is_continue_request,
    prepare_chat_context,
    resolve_loop_final_content,
    restore_continue_messages,
    trim_messages,
    inject_knowledge_context,
    run_tool_call_loop,
)

from nbot.core.chat_models import (
    ChatRequest,
    ChatResponse,
)

from nbot.core.model_adapter import (
    ProviderProfile,
    NormalizedModelResponse,
    build_chat_completion_payload,
    extract_reasoning_content,
    infer_provider_profile,
    normalize_messages_for_provider,
    normalize_chat_completion_data,
    parse_tool_call_arguments,
    parse_tool_calls,
    resolve_chat_completion_url,
)

from nbot.core.session_store import (
    QQSessionStore,
    WebSessionStore,
    build_chat_message,
    build_cli_session_id,
    build_qq_history_key,
    build_qq_session_id,
    dump_json,
)

__all__ = [
    # prompt 模块
    'PromptManager',
    'prompt_manager',
    'load_prompt',
    'load_memories',
    'add_memory',
    'save_prompt',
    'get_memories',
    'delete_memory',
    'clear_memories',
    
    # message 模块
    'Message',
    'MessageManager',
    'message_manager',
    'add_message',
    'get_messages',
    'create_message',
    'migrate_messages',
    
    # workspace 模块
    'WorkspaceManager',
    'workspace_manager',
    'ChatRequest',
    'ChatResponse',
    'AgentService',
    'ProviderProfile',
    'NormalizedModelResponse',

    'ToolLoopExit',
    'ToolLoopHooks',
    'ToolLoopSession',
    'PreparedChatContext',
    'ToolExecutionResult',
    'ToolLoopResult',
    'apply_tool_call_history',
    'build_continue_chat_response',
    'clean_response_content',
    'execute_tool_loop_session',
    'expand_hidden_tool_history',
    'run_tool_loop_session',
    'extract_display_text',
    'extract_tool_call_history',
    'normalize_chat_response',
    'prepare_chat_context',
    'resolve_loop_final_content',
    'is_continue_request',
    'restore_continue_messages',
    'trim_messages',
    'inject_knowledge_context',
    'run_tool_call_loop',
    'build_chat_completion_payload',
    'extract_reasoning_content',
    'infer_provider_profile',
    'normalize_messages_for_provider',
    'normalize_chat_completion_data',
    'parse_tool_call_arguments',
    'parse_tool_calls',
    'resolve_chat_completion_url',
    'QQSessionStore',
    'WebSessionStore',
    'build_chat_message',
    'build_cli_session_id',
    'build_qq_history_key',
    'build_qq_session_id',
    'dump_json',
]
