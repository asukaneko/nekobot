import asyncio
import logging

import commands
from commands import command_handlers, bot

logging.getLogger().setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)


class DummyAPI:
    def __getattr__(self, name):
        async def _method(*args, **kwargs):
            print(f"[bot.api.{name}] args={args}, kwargs={kwargs}")

        return _method


bot.api = DummyAPI()


class FakeMessage:
    def __init__(self, raw_message: str, user_id: int = 123456, group_id: int = 654321):
        self.raw_message = raw_message
        self.user_id = user_id
        self.group_id = group_id

    async def reply(self, **kwargs):
        print(f"[msg.reply] {kwargs}")


def find_handler(raw_message: str):
    if not raw_message:
        return None
    cmd = raw_message.split()[0]
    for aliases, func in command_handlers.items():
        if cmd in aliases:
            return func
    return None


async def run_command(raw_message: str, is_group: bool = True, user_id: int = 123456, group_id: int = 654321):
    handler = find_handler(raw_message)
    if not handler:
        print(f"未找到对应命令: {raw_message}")
        return
    msg = FakeMessage(raw_message, user_id=user_id, group_id=group_id)
    print(f"调用命令处理函数: {handler.__name__}, is_group={is_group}")
    await handler(msg, is_group=is_group)


async def run_chat(raw_message: str, is_group: bool = True, user_id: int = 123456, group_id: int = 654321):
    if is_group:
        content = commands.chat(
            content=raw_message,
            user_id=None,
            group_id=group_id,
            group_user_id=str(user_id),
            image=False,
            url=None,
            video=None,
        )
    else:
        content = commands.chat(
            content=raw_message,
            user_id=user_id,
            group_id=None,
            group_user_id=None,
            image=False,
            url=None,
            video=None,
        )

    try:
        parse = getattr(commands, "safe_parse_chat_response", None)
        if parse is not None:
            content, cmds = parse(content)
        else:
            cmds = []
    except Exception:
        cmds = []
    print(f"[AI] {content}")
    if cmds:
        print(f"[AI 内嵌命令] {cmds}")
        for cmd in cmds:
            if isinstance(cmd, str):
                cmd_str = cmd.strip()
                if cmd_str:
                    print(f"[AI 触发命令] {cmd_str}")
                    await run_command(cmd_str, is_group=is_group, user_id=user_id, group_id=group_id)


def main():
    print("commands 测试工具")
    print("输入 QQ 号与模式后，可以直接输入如 `/fb 书名`、`/fa 作者` 等命令进行测试")
    mode = input("测试模式 [g=群聊 / p=私聊] (默认 g): ").strip().lower() or "g"
    is_group = False if mode == "p" else True
    user_id_str = input("user_id (默认 123456): ").strip() or "123456"
    group_id_str = input("group_id (默认 654321): ").strip() or "654321"
    try:
        user_id = int(user_id_str)
    except ValueError:
        user_id = 123456
    try:
        group_id = int(group_id_str)
    except ValueError:
        group_id = 654321

    print("输入要测试的命令，输入 exit/quit/q 退出。")
    while True:
        raw = input("> ").strip()
        if not raw:
            continue
        if raw.lower() in ("exit", "quit", "q"):
            break
        if raw.startswith("/"):
            asyncio.run(run_command(raw, is_group=is_group, user_id=user_id, group_id=group_id))
        else:
            asyncio.run(run_chat(raw, is_group=is_group, user_id=user_id, group_id=group_id))


if __name__ == "__main__":
    main()
