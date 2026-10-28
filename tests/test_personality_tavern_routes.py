"""角色卡导入导出路由测试（酒馆生态）"""

import io
import json
import os

import pytest
from flask import Flask

from nbot.character import tavern_card
from nbot.web.routes.personality import register_personality_routes


class _StubServer:
    def __init__(self, base_dir):
        self.base_dir = str(base_dir)
        self.data_dir = os.path.join(self.base_dir, "data", "web")
        os.makedirs(self.data_dir, exist_ok=True)
        self.personality = {"name": "", "firstMessage": "", "tags": []}
        self.custom_personality_presets = []
        self.saved_keys = []

    def _save_data(self, key):
        self.saved_keys.append(key)


@pytest.fixture()
def client(tmp_path):
    app = Flask(__name__)
    server = _StubServer(tmp_path)
    register_personality_routes(app, server)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        test_client.stub_server = server
        yield test_client


def _v2_card_bytes(name="酒馆少女"):
    card = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": "来自酒馆",
            "personality": "活泼",
            "scenario": "海边",
            "first_mes": "海风真舒服。",
            "mes_example": "<user>你好",
            "alternate_greetings": ["（挥手）这边！", "今天浪好大。"],
            "tags": ["夏日"],
            "character_book": {
                "name": "海边小镇",
                "entries": [{"keys": ["灯塔"], "content": "灯塔在悬崖上。"}],
            },
        },
    }
    return json.dumps(card, ensure_ascii=False).encode("utf-8")


def _post_file(client, url, payload, filename):
    return client.post(
        url,
        data={"file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def test_import_tavern_v2_json(client):
    res = _post_file(client, "/api/personality/import", _v2_card_bytes(), "card.json")
    body = res.get_json()

    assert res.status_code == 200
    assert body["success"] is True
    character = body["character"]
    assert character["name"] == "酒馆少女"
    assert character["firstMessage"] == "海风真舒服。"
    assert character["alternateGreetings"] == ["（挥手）这边！", "今天浪好大。"]
    assert character["systemPrompt"]
    assert character["state"]["affection"] == 50

    meta = body["meta"]
    assert meta["format"] == "tavern_v2"
    assert meta["character_book"]["entries"][0]["keywords"] == ["灯塔"]


def test_import_tavern_png_sets_portrait(client, tmp_path):
    native = {"name": "PNG 少女", "description": "d", "firstMessage": "你好"}
    png = tavern_card.build_card_png(native)

    res = _post_file(client, "/api/personality/import", png, "card.png")
    body = res.get_json()

    assert res.status_code == 200
    assert body["success"] is True
    assert body["character"]["portrait"].startswith("/static/uploads/portraits/")
    saved = os.path.join(
        str(tmp_path), "nbot", "web", "static",
        body["character"]["portrait"].replace("/static/", "").replace("/", os.sep),
    )
    assert os.path.exists(saved)
    assert body["meta"]["format"].startswith("png_")


def test_import_rejects_unknown_format(client):
    res = _post_file(client, "/api/personality/import", b"hello", "card.txt")
    assert res.status_code == 400
    assert res.get_json()["success"] is False


def test_export_tavern_v3_json(client):
    character = {
        "name": "导出角色",
        "description": "d",
        "firstMessage": "hi",
        "alternateGreetings": ["a1"],
        "tags": ["t"],
    }
    res = client.post("/api/personality/export", json={"character": character, "format": "tavern_v3"})

    assert res.status_code == 200
    payload = json.loads(res.data.decode("utf-8"))
    assert payload["spec"] == "chara_card_v3"
    assert payload["data"]["name"] == "导出角色"
    assert payload["data"]["alternate_greetings"] == ["a1"]


def test_export_tavern_png_without_portrait(client):
    character = {"name": "无立绘角色", "firstMessage": "hi"}
    res = client.post(
        "/api/personality/export", json={"character": character, "format": "tavern_png"}
    )

    assert res.status_code == 200
    assert res.mimetype == "image/png"
    payload = tavern_card.extract_card_payload(res.data)
    assert payload["data"]["name"] == "无立绘角色"


def test_export_tavern_uses_linked_world_book(client):
    book_res = client.post(
        "/api/personality/import-character-book",
        json={
            "character_id": "char-1",
            "book": {
                "name": "世界观",
                "entries": [{"name": "魔法", "keywords": ["魔法"], "content": "需要咏唱"}],
            },
        },
    )
    book_id = book_res.get_json()["world_book"]["id"]

    export_res = client.post(
        "/api/personality/export",
        json={
            "character": {"name": "带世界书", "firstMessage": "hi"},
            "format": "tavern_v2",
            "world_book_id": book_id,
        },
    )
    payload = json.loads(export_res.data.decode("utf-8"))
    entries = payload["data"]["character_book"]["entries"]
    assert entries[0]["keys"] == ["魔法"]
    assert entries[0]["content"] == "需要咏唱"


def test_import_character_book_creates_world_book(client, tmp_path):
    res = client.post(
        "/api/personality/import-character-book",
        json={
            "character_id": "char-9",
            "character_name": "阿岚",
            "book": {
                "name": "边境小镇",
                "entries": [
                    {"name": "镇口", "keywords": ["镇口"], "content": "镇口有棵老树。"},
                    {"name": "酒馆", "keywords": ["酒馆"], "content": "酒馆很热闹。"},
                ],
            },
        },
    )
    body = res.get_json()
    assert res.status_code == 200
    assert body["entry_count"] == 2

    store_path = os.path.join(str(tmp_path), "data", "world_books.json")
    with open(store_path, encoding="utf-8") as f:
        stored = json.load(f)
    book = stored["world_books"][body["world_book"]["id"]]
    assert book["name"] == "边境小镇"
    assert book["character_ids"] == ["char-9"]
    assert len(book["entries"]) == 2


def test_import_character_book_requires_entries(client):
    res = client.post(
        "/api/personality/import-character-book", json={"book": {"name": "空书", "entries": []}}
    )
    assert res.status_code == 400


def test_greeting_pick_by_index_and_random(client):
    character = {
        "name": "小春",
        "firstMessage": "{{char}}来啦，{{user}}。",
        "alternateGreetings": ["第二条", "第三条"],
    }

    res = client.post("/api/personality/greeting", json={"character": character, "index": 0})
    body = res.get_json()
    assert body["text"].startswith("小春来啦")
    assert body["count"] == 3

    res = client.post(
        "/api/personality/greeting",
        json={"character": character, "index": 2, "user": "阿明"},
    )
    assert res.get_json()["text"] == "第三条"

    res = client.post("/api/personality/greeting", json={"character": character, "random": True})
    assert res.get_json()["text"] in {
        "小春来啦，{{user}}。",
        "第二条",
        "第三条",
    }
