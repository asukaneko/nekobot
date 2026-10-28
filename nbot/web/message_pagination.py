"""聊天历史的有界分页；无分页参数时仍由路由返回完整列表。"""


def message_page(messages, limit, before_message_id=None):
    limit = int(limit)
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    end = len(messages)
    if before_message_id is not None:
        end = next(
            (i for i, item in enumerate(messages) if str(item.get("id")) == before_message_id),
            -1,
        )
        if end < 0:
            raise LookupError("Message cursor not found")
    start = max(0, end - limit)
    return {"messages": messages[start:end], "has_more": start > 0}
