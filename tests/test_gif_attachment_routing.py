from types import SimpleNamespace

from nbot.core.message_middleware import (
    MediaDescriber,
    MessagePreprocessor,
    _is_gif_attachment,
)
from nbot.services.ai import _ensure_remote_media_url, _read_media_bytes


def test_is_gif_attachment_detects_sources():
    assert _is_gif_attachment({"mime_type": "image/gif"}, "https://x/y")
    assert _is_gif_attachment({}, "data:image/gif;base64,AAAA")
    assert _is_gif_attachment({"file": "emoticon.gif"}, "https://x/y")
    assert _is_gif_attachment({}, "https://x/y/emo.gif?v=1")
    assert not _is_gif_attachment({"mime_type": "image/png"}, "https://x/y.png")
    assert not _is_gif_attachment({}, "")


def test_read_media_bytes_from_data_url():
    assert _read_media_bytes("data:image/gif;base64,QUJD") == b"ABC"


def test_ensure_remote_media_url_keeps_http_and_data():
    assert _ensure_remote_media_url("https://x/a.gif") == "https://x/a.gif"
    assert _ensure_remote_media_url("data:image/png;base64,QQ==").startswith("data:image/png")


def test_preprocessor_routes_gif_to_gif_describer():
    original_gif = MediaDescriber._describers.get("gif")
    original_image = MediaDescriber._describers.get("image")
    seen = {}

    def fake_gif(url):
        seen["gif_url"] = url
        return "一个跳动的表情"

    def fake_image(url):
        seen["image_url"] = url
        return "静态图片"

    MediaDescriber.register("gif", fake_gif)
    MediaDescriber.register("image", fake_image)
    try:
        request = SimpleNamespace(
            channel="qq",
            content="",
            attachments=[
                {
                    "type": "image",
                    "source": "qq",
                    "data": "data:image/gif;base64,QUJD",
                }
            ],
        )
        MessagePreprocessor.process(request)
        assert seen["gif_url"] == "data:image/gif;base64,QUJD"
        assert "image_url" not in seen
        assert "[动图1描述]: 一个跳动的表情" in request.content
    finally:
        if original_gif is not None:
            MediaDescriber.register("gif", original_gif)
        else:
            MediaDescriber._describers.pop("gif", None)
        if original_image is not None:
            MediaDescriber.register("image", original_image)
