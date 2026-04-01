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
    ToolLoopExit,
    ToolLoopHooks,
    ToolLoopResult,
    is_continue_request,
    restore_continue_messages,
    trim_messages,
    inject_knowledge_context,
    run_tool_call_loop,
)

from nbot.core.chat_models import (
    ChatRequest,
    ChatResponse,
)

from nbot.core.session_store import (
    QQSessionStore,
    WebSessionStore,
    build_chat_message,
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

    'ToolLoopExit',
    'ToolLoopHooks',
    'ToolLoopResult',
    'is_continue_request',
    'restore_continue_messages',
    'trim_messages',
    'inject_knowledge_context',
    'run_tool_call_loop',
    'QQSessionStore',
    'WebSessionStore',
    'build_chat_message',
    'build_qq_history_key',
    'build_qq_session_id',
    'dump_json',
]
