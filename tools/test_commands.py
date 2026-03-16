import asyncio
import logging
import sys
import os
import cmd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nbot import commands
from nbot.services.ai import ai_client, user_messages, group_messages
from nbot.services.chat_service import chat as do_chat, load_prompt

logging.basicConfig(level=logging.INFO)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

bot = commands.bot


class NekoBotShell(cmd.Cmd):
    intro = """
╔══════════════════════════════════════════╗
║     NekoBot Test Shell                    ║
║     输入 help 查看可用命令                ║
╚══════════════════════════════════════════╝
"""
    prompt = "NekoBot> "

    def __init__(self):
        super().__init__()
        self.current_user_id = "test_user"
        self.current_group_id = None

    def do_start(self, arg):
        """启动机器人"""
        print("启动 NekoBot...")
        bot.run()

    def do_chat(self, arg):
        """chat <消息> - 与 AI 对话"""
        if not arg.strip():
            print("用法: chat <消息>")
            return

        print(f"[用户]: {arg}")

        async def run_chat():
            response = await do_chat(
                message=arg,
                user_id=self.current_user_id,
                group_id=self.current_group_id
            )
            print(f"[Bot]: {response}")

        asyncio.run(run_chat())

    def do_history(self, arg):
        """history [user|group] - 查看聊天历史"""
        target = user_messages if self.current_group_id is None else group_messages
        target_id = self.current_user_id if self.current_group_id is None else self.current_group_id

        if arg:
            if arg == "user":
                target = user_messages
                target_id = self.current_user_id
            elif arg == "group":
                target = group_messages
                if self.current_group_id:
                    target_id = self.current_group_id
                else:
                    print("未设置群号")
                    return

        if target_id in target:
            msgs = target[target_id]
            print(f"=== {target_id} 的聊天历史 ({len(msgs)} 条) ===")
            for i, msg in enumerate(msgs[-10:]):
                role = msg.get("role", "?")
                content = msg.get("content", "")[:50]
                print(f"{i+1}. [{role}]: {content}...")
        else:
            print("暂无历史记录")

    def do_clear(self, arg):
        """clear - 清除当前会话历史"""
        target = user_messages if self.current_group_id is None else group_messages
        target_id = self.current_user_id if self.current_group_id is None else self.current_group_id

        if target_id in target:
            del target[target_id]
            print(f"已清除 {target_id} 的聊天历史")
        else:
            print("暂无历史记录")

    def do_set_user(self, arg):
        """set_user <user_id> - 设置当前用户ID"""
        if not arg.strip():
            print("用法: set_user <user_id>")
            return
        self.current_user_id = arg.strip()
        self.current_group_id = None
        print(f"当前用户: {self.current_user_id}, 群: (私聊)")

    def do_set_group(self, arg):
        """set_group <group_id> - 设置当前群ID"""
        if not arg.strip():
            print("用法: set_group <group_id>")
            return
        self.current_group_id = arg.strip()
        print(f"当前用户: {self.current_user_id}, 群: {self.current_group_id}")

    def do_prompt(self, arg):
        """prompt - 查看当前 Prompt"""
        prompt = load_prompt(
            user_id=self.current_user_id if not self.current_group_id else None,
            group_id=self.current_group_id
        )
        print("=== 当前 System Prompt ===")
        print(prompt[:500] + "..." if len(prompt) > 500 else prompt)

    def do_skills(self, arg):
        """skills - 查看可用技能"""
        try:
            from nbot.plugins import get_plugin_manager
            pm = get_plugin_manager()
            from nbot.plugins.dispatcher import get_skill_dispatcher
            dispatcher = get_skill_dispatcher(pm)
            skills = dispatcher.get_available_skills_prompt()
            print("=== 可用技能 ===")
            print(skills)
        except Exception as e:
            print(f"获取技能失败: {e}")

    def do_memory(self, arg):
        """memory [add|search|recall] - 记忆功能测试"""
        try:
            from nbot.core.memory import get_memory_manager
            mm = get_memory_manager()

            parts = arg.strip().split(None, 1)
            if not parts:
                print("用法: memory add <内容> | search <关键词> | recall")
                return

            cmd = parts[0]
            content = parts[1] if len(parts) > 1 else ""

            if cmd == "add":
                mm.remember(content=content, user_id=self.current_user_id)
                print(f"已记住: {content}")
            elif cmd == "search":
                results = mm.recall(user_id=self.current_user_id, query=content)
                print("=== 记忆搜索结果 ===")
                print(results)
            elif cmd == "recall":
                results = mm.recall(user_id=self.current_user_id)
                print("=== 所有记忆 ===")
                print(results)
            else:
                print("用法: memory add <内容> | search <关键词> | recall")
        except Exception as e:
            print(f"记忆功能错误: {e}")

    def do_knowledge(self, arg):
        """knowledge [add|search] - 知识库功能测试"""
        try:
            from nbot.core.knowledge import get_knowledge_manager
            km = get_knowledge_manager()

            parts = arg.strip().split(None, 2)
            if not parts:
                print("用法: knowledge add <标题>|<内容> | search <关键词>")
                return

            cmd = parts[0]

            if cmd == "add":
                if "|" not in parts[1]:
                    print("用法: knowledge add <标题>|<内容>")
                    return
                title, content = parts[1].split("|", 1)
                kb = km.create_knowledge_base(name="测试知识库", user_id=self.current_user_id)
                km.add_document(base_id=kb.id, title=title, content=content)
                print(f"已添加文档: {title}")
            elif cmd == "search":
                results = km.search(query=parts[1], user_id=self.current_user_id)
                print("=== 知识库搜索结果 ===")
                for doc, score, chunk in results:
                    print(f"- {doc.title} (相似度: {score:.2f})")
                    print(f"  {chunk[:100]}...")
            else:
                print("用法: knowledge add <标题>|<内容> | search <关键词>")
        except Exception as e:
            print(f"知识库错误: {e}")

    def do_workflow(self, arg):
        """workflow [list|run] - 工作流功能测试"""
        try:
            from nbot.core.workflow import get_workflow_engine
            engine = get_workflow_engine()

            if not arg.strip():
                wfs = engine.list_workflows()
                print("=== 工作流列表 ===")
                for wf in wfs:
                    print(f"- {wf['name']}: {wf['description']} (启用: {wf['enabled']})")
                return

            parts = arg.strip().split()
            cmd = parts[0]

            if cmd == "list":
                wfs = engine.list_workflows()
                for wf in wfs:
                    print(f"- {wf['name']}: {wf['description']}")
            elif cmd == "run":
                if len(parts) < 2:
                    print("用法: workflow run <工作流名>")
                    return
                wf_name = parts[1]
                result = asyncio.run(engine.execute_workflow(wf_name, {"test": "data"}))
                print(f"=== 工作流执行结果 ===")
                print(f"状态: {result.status}")
                print("日志:")
                for log in result.logs:
                    print(f"  {log}")
            else:
                print("用法: workflow [list|run <名称>]")
        except Exception as e:
            print(f"工作流错误: {e}")

    def do_config(self, arg):
        """config - 查看当前配置"""
        try:
            from nbot.utils.config_loader import load_config
            config = load_config()
            print("=== 当前配置 ===")
            for section in config.sections():
                print(f"[{section}]")
                for key, value in config.items(section):
                    print(f"  {key} = {value}")
        except Exception as e:
            print(f"配置错误: {e}")

    def do_test_all(self, arg):
        """test_all - 运行所有功能测试"""
        print("\n" + "="*50)
        print("开始功能测试...")
        print("="*50 + "\n")

        print("[1] 测试 AI 对话...")
        self.do_chat("你好")

        print("\n[2] 测试技能列表...")
        self.do_skills("")

        print("\n[3] 测试记忆功能...")
        self.do_memory("add 测试记忆内容")
        self.do_memory("recall")

        print("\n[4] 测试知识库...")
        self.do_knowledge("add 测试标题|这是测试内容")

        print("\n[5] 测试工作流...")
        self.do_workflow("list")

        print("\n" + "="*50)
        print("测试完成!")
        print("="*50)

    def do_exit(self, arg):
        """退出"""
        print("再见!")
        return True

    def do_quit(self, arg):
        """退出"""
        return self.do_exit(arg)

    def do_EOF(self, arg):
        """退出 (Ctrl+D)"""
        print()
        return True


def main():
    shell = NekoBotShell()

    if len(sys.argv) > 1:
        if sys.argv[1] == "start":
            print("启动 NekoBot 服务...")
            bot.run()
        elif sys.argv[1] == "shell":
            shell.cmdloop()
        else:
            print("用法: python test_commands.py [start|shell]")
    else:
        print("""
用法:
  python test_commands.py start  - 启动机器人服务
  python test_commands.py shell  - 交互式测试 Shell

在 Shell 中可以使用:
  chat <消息>           - 与 AI 对话
  history               - 查看聊天历史
  clear                 - 清除历史
  set_user <id>         - 设置用户
  set_group <id>       - 设置群
  prompt                - 查看当前 Prompt
  skills                - 查看可用技能
  memory add <内容>     - 添加记忆
  memory recall         - 回忆
  knowledge add <标题>|<内容> - 添加知识
  knowledge search <词> - 搜索知识
  workflow list         - 列出工作流
  config                - 查看配置
  test_all              - 运行所有测试
  exit/quit             - 退出
""")

if __name__ == "__main__":
    main()
