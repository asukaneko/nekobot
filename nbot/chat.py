# 兼容层：从新位置导入并导出所有功能
from nbot.services.ai import (
    AIClient,
    ai_client,
    user_messages,
    group_messages,
    api_key,
    base_url,
    model,
    MAX_HISTORY_LENGTH,
    pic_model,
    search_api_key,
    search_api_url,
    video_api,
    silicon_api_key,
)

from nbot.services.chat_service import (
    remove_brackets_content,
    load_prompt,
    online_search,
    chat_image,
    chat_gif,
    chat_video,
    chat_webpage,
    chat_json,
    judge_reply,
    chat,
    record_assistant_message,
    record_user_message,
    log_to_group_full_file,
    summarize_group_text,
    generate_today_summary,
)

from nbot.services.tts import (
    tts,
    upload_voice,
)
