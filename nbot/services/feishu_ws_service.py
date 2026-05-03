"""飞书 WebSocket 长连接服务

使用飞书官方 SDK (lark-oapi) 建立 WebSocket 长连接，无需公网地址即可接收消息。

使用方式:
1. 在飞书开放平台创建企业自建应用
2. 在"事件与回调"页面选择"使用长连接接收事件"
3. 订阅需要的事件（如 im.message.receive_v1）
4. 配置 App ID 和 App Secret
5. 启动服务即可接收消息

注意:
- 长连接模式仅支持企业自建应用
- 每个应用最多建立 50 个连接
- 需要在 3 秒内处理完消息，否则会触发重推机制
"""

import json
import os
import threading
from typing import Any, Callable, Dict, List, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import *


class FeishuWebSocketService:
    """飞书 WebSocket 长连接服务"""

    def __init__(self):
        self._clients: Dict[str, lark.ws.Client] = {}
        self._server: Optional[Any] = None
        self._lock = threading.Lock()

    def set_server(self, server: Any):
        """设置服务器实例，用于处理消息"""
        self._server = server

    def _handle_message(self, app_id: str, event_data: Any) -> None:
        """处理接收到的消息事件"""
        try:
            if not self._server:
                print("[FeishuWS] 服务器实例未设置，无法处理消息")
                return

            # 提取消息信息
            message = event_data.message
            sender = event_data.sender

            # 解析消息内容
            content = message.content
            if isinstance(content, str):
                try:
                    content_obj = json.loads(content)
                    text = content_obj.get("text", "")
                except json.JSONDecodeError:
                    text = content
            else:
                text = str(content)

            # 查找对应的频道配置
            channel_config = None
            for channel in self._server.channels_config:
                if channel.get("type") == "feishu_ws":
                    config = channel.get("config") or {}
                    credentials = resolve_feishu_ws_credentials(config)
                    if credentials["app_id"] == app_id:
                        channel_config = channel
                        break

            if not channel_config:
                print(f"[FeishuWS] 未找到匹配的频道配置 [App ID: {app_id}]")
                return

            # 构建标准化消息格式
            parsed_message = {
                "app_id": app_id,
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "message_id": message.message_id,
                "message_type": message.message_type,
                "user_id": sender.sender_id.open_id if sender and sender.sender_id else "",
                "sender": sender.sender_id.open_id if sender and sender.sender_id else "feishu_user",
                "content": text.strip(),
                "create_time": message.create_time,
                "update_time": message.update_time,
                "metadata": {
                    "feishu_event_type": "im.message.receive_v1",
                    "feishu_chat_id": message.chat_id,
                    "feishu_chat_type": message.chat_type,
                    "feishu_message_id": message.message_id,
                    "feishu_message_type": message.message_type,
                    "feishu_sender_id": sender.sender_id.open_id if sender and sender.sender_id else "",
                    "feishu_app_id": app_id,
                },
            }

            # 在后台线程处理消息
            def process_message():
                try:
                    # 传入 config 字段而不是整个 channel 对象
                    handle_feishu_message(self._server, channel_config.get("config", {}), parsed_message)
                except Exception as e:
                    print(f"[FeishuWS] 处理消息失败: {e}")
                    import traceback
                    traceback.print_exc()

            threading.Thread(target=process_message, daemon=True).start()

        except Exception as e:
            print(f"[FeishuWS] 处理消息事件失败: {e}")
            import traceback
            traceback.print_exc()

    def create_client(
        self,
        app_id: str,
        app_secret: str,
        encrypt_key: Optional[str] = None,
        verification_token: Optional[str] = None,
    ) -> Optional[lark.ws.Client]:
        """创建飞书 WebSocket 客户端"""
        try:
            # 创建事件处理器 - 使用正确的方式注册事件处理函数
            builder = lark.EventDispatcherHandler.builder(
                encrypt_key=encrypt_key or "",
                verification_token=verification_token or "",
            )

            # 注册消息接收事件处理函数
            # 使用闭包捕获 app_id
            app_id_capture = app_id
            builder.register_p2_im_message_receive_v1(
                lambda data: self._handle_message(app_id_capture, data.event)
            )

            # 注册用户进入私聊事件（可选，用于避免报错）
            try:
                builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
                    lambda data: None  # 空处理，不执行任何操作
                )
            except Exception:
                # 如果 SDK 不支持这个事件，忽略错误
                pass

            # 注册消息已读事件（避免报错日志）
            try:
                builder.register_p2_im_message_message_read_v1(
                    lambda data: None  # 空处理，不执行任何操作
                )
            except Exception:
                pass

            event_handler = builder.build()

            # 创建 WebSocket 客户端
            client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,  # 使用 INFO 级别减少日志输出
            )

            return client

        except Exception as e:
            print(f"[FeishuWS] 创建客户端失败 [{app_id}]: {e}")
            import traceback
            traceback.print_exc()
            return None

    def start_client(
        self,
        channel_id: str,
        app_id: str,
        app_secret: str,
        encrypt_key: Optional[str] = None,
        verification_token: Optional[str] = None,
    ) -> bool:
        """启动指定频道的 WebSocket 客户端"""
        if not app_id or not app_secret:
            print(f"[FeishuWS] 缺少 App ID 或 App Secret，无法启动客户端 [{channel_id}]")
            return False

        with self._lock:
            # 如果该频道已有客户端，先停止
            if channel_id in self._clients:
                self.stop_client(channel_id)

            # 创建新客户端
            client = self.create_client(app_id, app_secret, encrypt_key, verification_token)
            if not client:
                return False

            self._clients[channel_id] = client

        # 在后台线程启动客户端
        def run_client():
            try:
                print(f"[FeishuWS] 正在启动客户端 [{channel_id}]...")
                print(f"[FeishuWS] App ID: {app_id}")
                # 启动客户端（阻塞方法）
                client.start()
            except Exception as e:
                print(f"[FeishuWS] 客户端运行异常 [{channel_id}]: {e}")
                import traceback
                traceback.print_exc()
            finally:
                with self._lock:
                    if channel_id in self._clients:
                        del self._clients[channel_id]
                print(f"[FeishuWS] 客户端已停止 [{channel_id}]")

        thread = threading.Thread(target=run_client, daemon=True)
        thread.start()

        print(f"[FeishuWS] 客户端启动线程已创建 [{channel_id}]")
        return True

    def stop_client(self, channel_id: str) -> bool:
        """停止指定频道的 WebSocket 客户端"""
        with self._lock:
            client = self._clients.get(channel_id)
            if not client:
                return False

            try:
                client.stop()
                del self._clients[channel_id]
                print(f"[FeishuWS] 客户端已停止 [{channel_id}]")
                return True
            except Exception as e:
                print(f"[FeishuWS] 停止客户端失败 [{channel_id}]: {e}")
                return False

    def stop_all(self):
        """停止所有客户端"""
        with self._lock:
            channel_ids = list(self._clients.keys())

        for channel_id in channel_ids:
            self.stop_client(channel_id)

        print("[FeishuWS] 所有客户端已停止")

    def is_running(self, channel_id: str) -> bool:
        """检查指定频道的客户端是否正在运行"""
        with self._lock:
            return channel_id in self._clients

    def list_running_clients(self) -> list:
        """获取所有正在运行的客户端频道 ID 列表"""
        with self._lock:
            return list(self._clients.keys())


# 全局服务实例
feishu_ws_service = FeishuWebSocketService()


def resolve_feishu_ws_credentials(config: Dict[str, Any]) -> Dict[str, str]:
    """解析飞书 WebSocket 连接凭证

    支持两种方式配置:
    1. 直接填写值: app_id, app_secret
    2. 环境变量名: app_id_env, app_secret_env (优先)
    """
    def get_config(key: str, env_key: str, default_env: str = "") -> str:
        # 首先尝试从环境变量获取
        env_name = str(config.get(env_key) or "").strip()
        if env_name:
            value = os.getenv(env_name)
            if value:
                return value.strip()
        # 如果没有环境变量，尝试直接获取值
        direct_value = str(config.get(key) or "").strip()
        if direct_value:
            return direct_value
        # 最后尝试默认环境变量
        if default_env:
            value = os.getenv(default_env)
            if value:
                return value.strip()
        return ""

    return {
        "app_id": get_config("app_id", "app_id_env", "FEISHU_APP_ID"),
        "app_secret": get_config("app_secret", "app_secret_env", "FEISHU_APP_SECRET"),
        "encrypt_key": get_config("encrypt_key", "encrypt_key_env", "FEISHU_ENCRYPT_KEY"),
        "verification_token": get_config(
            "verification_token", "verification_token_env", "FEISHU_VERIFICATION_TOKEN"
        ),
    }


def send_feishu_reply(
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    reply_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """发送飞书回复消息"""
    try:
        # 创建客户端
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        # 构建请求
        content = json.dumps({"text": text}, ensure_ascii=False)

        # 构建消息请求
        request_body = CreateMessageRequestBody.builder()
        request_body.receive_id(chat_id)
        request_body.msg_type("text")
        request_body.content(content)

        if reply_message_id:
            request_body.reply_in_thread(True)

        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(request_body.build()) \
            .build()

        # 发送请求
        response: CreateMessageResponse = client.im.v1.message.create(request)

        if response.success():
            return {
                "success": True,
                "code": response.code,
                "msg": response.msg,
                "data": response.data,
            }
        else:
            return {
                "success": False,
                "code": response.code,
                "msg": response.msg,
                "error": response.error,
            }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def handle_feishu_message(
    server: Any,
    config: Dict[str, Any],
    message: Dict[str, Any],
) -> Dict[str, Any]:
    """处理飞书消息 - 使用完整的 Web 功能"""
    from nbot.services.feishu_chat_service import FeishuChatService

    credentials = resolve_feishu_ws_credentials(config)

    if not credentials["app_id"] or not credentials["app_secret"]:
        print(f"[FeishuWS] 未配置飞书应用凭证 [App ID: {credentials.get('app_id', 'empty')}]")
        return {"success": False, "error": "未配置飞书应用凭证"}

    # 获取频道 ID
    channel_id = None
    channel_name = "飞书"
    for ch in server.channels_config:
        if ch.get("type") == "feishu_ws":
            ch_config = ch.get("config") or {}
            ch_credentials = resolve_feishu_ws_credentials(ch_config)
            if ch_credentials["app_id"] == credentials["app_id"]:
                channel_id = ch.get("id")
                channel_name = ch.get("name", "飞书")
                break

    if not channel_id:
        channel_id = "feishu_ws"

    print(f"[FeishuWS] 处理消息 [频道: {channel_id}, 用户: {message.get('user_id', 'unknown')}]")

    # 使用聊天服务处理消息
    chat_service = FeishuChatService(server)
    chat_service.handle_message(channel_id, config, message, credentials)

    return {"success": True}
