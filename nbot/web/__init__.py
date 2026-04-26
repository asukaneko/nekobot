__all__ = ["WebChatServer", "create_web_app"]


def __getattr__(name):
    if name in __all__:
        from nbot.web.server import WebChatServer, create_web_app

        exports = {
            "WebChatServer": WebChatServer,
            "create_web_app": create_web_app,
        }
        return exports[name]
    raise AttributeError(name)
