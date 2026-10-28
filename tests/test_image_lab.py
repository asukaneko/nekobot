"""P2-10 图生图 / 生图实验室后端测试。"""

import json
import os
import threading
import time
import types

import pytest
from flask import Flask

from nbot.services import image_service
from nbot.web.routes.images import register_image_routes


def _config():
    return {
        "api_key": "test-key",
        "base_url": "https://api.example.test/v1/images/generations",
        "proxy_url": "",
        "model": "test-image-model",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "append_base_url_path": True,
        "size": "",
    }


@pytest.fixture
def history_path(tmp_path, monkeypatch):
    path = tmp_path / "image_history.json"
    monkeypatch.setenv("NBOT_IMAGE_HISTORY_PATH", str(path))
    return path


@pytest.fixture
def templates_path(tmp_path, monkeypatch):
    path = tmp_path / "image_lab_templates.json"
    monkeypatch.setenv("NBOT_IMAGE_TEMPLATES_PATH", str(path))
    return path


@pytest.fixture
def client(history_path, templates_path, tmp_path):
    app = Flask(__name__)
    server = types.SimpleNamespace(
        ai_models=[],
        active_models_by_purpose={},
        data_dir=str(tmp_path),
    )
    register_image_routes(app, server)
    app.config["TESTING"] = True
    return app.test_client()


def test_normalize_image_size_accepts_and_rejects():
    assert image_service.normalize_image_size("1024x1024") == "1024x1024"
    assert image_service.normalize_image_size("1024*768") == "1024x768"
    assert image_service.normalize_image_size("") == "1024x1024"
    for bad in ("abc", "100x100", "5000x5000"):
        with pytest.raises(ValueError):
            image_service.normalize_image_size(bad)


def test_normalize_strength_percent_and_clamp():
    assert image_service.normalize_strength(0.4) == 0.4
    assert image_service.normalize_strength(60) == 0.6
    assert image_service.normalize_strength(-1) == 0.0
    assert image_service.normalize_strength("bad") == image_service.DEFAULT_IMAGE_STRENGTH


def test_compose_prompt_includes_style_and_negative():
    prompt = image_service.compose_image_prompt("a cat", style="anime", negative_prompt="blurry")
    assert "a cat" in prompt
    assert "anime illustration" in prompt
    assert "avoid:" in prompt and "blurry" in prompt


def test_generate_txt2img_batch_records_history(monkeypatch, history_path):
    monkeypatch.setattr(
        image_service, "call_image_generation", lambda *a, **k: "data:image/png;base64,AAAA"
    )
    monkeypatch.setattr(
        image_service,
        "_save_generated_image",
        lambda data, prefix="": (
            "/static/uploads/image_lab/lab_1.png",
            os.path.join(
                image_service.get_static_root(), "uploads", "image_lab", "lab_1.png"
            ),
        ),
    )

    result = image_service.generate_image(
        "a cat",
        config=_config(),
        size="512x512",
        count=2,
        style="anime",
    )

    assert result["success"] is True
    assert len(result["images"]) == 2
    assert result["history_id"]

    payload = json.loads(history_path.read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["mode"] == "txt2img"
    assert entry["style"] == "anime"
    assert entry["count"] == 2
    assert entry["size"] == "512x512"
    assert len(entry["results"]) == 2
    assert entry["results"][0]["path"] == "uploads/image_lab/lab_1.png"
    assert entry["results"][0]["name"] == "lab_1.png"
    assert entry["results"][0]["status"] == "success"
    assert "api_key" not in json.dumps(entry)


def test_generate_img2img_passes_reference_and_strength(monkeypatch, history_path):
    captured = {}

    def fake_edit(prompt, config, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "data:image/png;base64,BBBB"

    monkeypatch.setattr(image_service, "call_image_to_image", fake_edit)
    monkeypatch.setattr(image_service, "load_reference_bytes", lambda source: b"img")
    monkeypatch.setattr(
        image_service,
        "_save_generated_image",
        lambda data, prefix="": ("/static/uploads/image_lab/lab_i2i_1.png", "/tmp/i2i.png"),
    )

    result = image_service.generate_image(
        "make it night",
        config=_config(),
        mode="img2img",
        reference_images=["/static/uploads/image_lab/references/a.png"],
        strength="75",
        size="768x768",
    )

    assert result["success"] is True
    assert captured["reference_images"] == ["/static/uploads/image_lab/references/a.png"]
    assert captured["strength"] == 0.75
    assert captured["size"] == "768x768"
    entry = json.loads(history_path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["mode"] == "img2img"
    assert entry["strength"] == 0.75
    assert entry["reference_images"]


def test_generate_img2img_requires_reference(monkeypatch, history_path):
    result = image_service.generate_image("x", config=_config(), mode="img2img")
    assert result["success"] is False
    assert result["reason"] == "missing_reference"


def test_quota_exceeded_blocks_generation(monkeypatch, history_path):
    monkeypatch.setattr(
        image_service,
        "get_image_quota_settings",
        lambda: {
            "daily_limit": 1,
            "max_images_per_request": 4,
            "max_reference_images": 4,
            "max_reference_bytes": 1024,
            "min_side": 256,
            "max_side": 4096,
        },
    )
    image_service.append_image_history({
        "id": "old",
        "created_at": image_service.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "txt2img",
        "status": "success",
        "results": [{"status": "success", "url": "/x.png", "path": "/x.png"}],
    })
    called = {"n": 0}

    def fake_gen(*args, **kwargs):
        called["n"] += 1
        return "data:image/png;base64,CCCC"

    monkeypatch.setattr(image_service, "call_image_generation", fake_gen)

    result = image_service.generate_image("a cat", config=_config())
    assert result["success"] is False
    assert result["reason"] == "quota_exceeded"
    assert result["quota"]["limit"] == 1
    assert called["n"] == 0


def test_history_list_and_delete(monkeypatch, history_path, tmp_path):
    file_path = tmp_path / "lab.png"
    file_path.write_bytes(b"png")
    monkeypatch.setattr(
        image_service,
        "get_image_lab_upload_dir",
        lambda: str(tmp_path),
    )
    image_service.append_image_history({
        "id": "e1",
        "created_at": "2026-10-25T10:00:00+08:00",
        "mode": "img2img",
        "status": "success",
        "prompt": "a dog",
        "results": [{"status": "success", "url": "/static/x.png", "path": str(file_path)}],
    })

    listed = image_service.list_image_history(mode="img2img", keyword="dog")
    assert listed["total"] == 1
    assert listed["entries"][0]["id"] == "e1"
    assert image_service.list_image_history(mode="txt2img")["total"] == 0

    assert image_service.delete_image_history_entry("e1", delete_files=True) is True
    assert image_service.get_image_history_entry("e1") is None
    assert not file_path.exists()


def test_styles_route_returns_presets(client):
    response = client.get("/api/images/styles")
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    style_ids = [item["id"] for item in body["styles"]]
    assert "anime" in style_ids and "none" in style_ids
    assert body["sizes"]
    assert body["limits"]["max_images_per_request"] >= 1


def test_generate_route_maps_quota_to_429(client, monkeypatch):
    monkeypatch.setattr(
        image_service,
        "generate_image",
        lambda *a, **k: {"success": False, "error": "quota", "reason": "quota_exceeded"},
    )
    response = client.post("/api/images/generate", json={"prompt": "a cat"})
    assert response.status_code == 429
    assert response.get_json()["reason"] == "quota_exceeded"


def test_generate_route_success(client, monkeypatch):
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"success": True, "images": [{"url": "/x.png"}], "history_id": "h1"}

    monkeypatch.setattr(image_service, "generate_image", fake_generate)
    response = client.post(
        "/api/images/generate",
        json={"prompt": "a cat", "mode": "img2img", "reference_images": ["a.png"], "count": 2},
    )
    assert response.status_code == 200
    assert captured["prompt"] == "a cat"
    assert captured["mode"] == "img2img"
    assert captured["count"] == 2


def test_history_routes_roundtrip(client, monkeypatch, tmp_path):
    image_service.append_image_history({
        "id": "h2",
        "created_at": "2026-10-25T10:00:00+08:00",
        "mode": "txt2img",
        "status": "success",
        "prompt": "a cat",
        "results": [{"status": "success", "url": "/x.png", "path": str(tmp_path / "nope.png")}],
    })

    listed = client.get("/api/images/history?limit=10").get_json()
    assert listed["total"] == 1
    assert listed["entries"][0]["id"] == "h2"

    detail = client.get("/api/images/history/h2")
    assert detail.status_code == 200
    assert detail.get_json()["entry"]["id"] == "h2"

    deleted = client.delete("/api/images/history/h2?keep_files=1")
    assert deleted.status_code == 200
    assert client.get("/api/images/history/h2").status_code == 404


def test_reference_upload_and_delete(client, monkeypatch, tmp_path):
    ref_dir = tmp_path / "references"
    monkeypatch.setattr(image_service, "get_image_reference_dir", lambda: str(ref_dir))

    data = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    response = client.post(
        "/api/images/references",
        data={"file": (io_bytes(data), "ref.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_json()
    name = body["reference"]["name"]
    assert (ref_dir / name).exists()

    listed = client.get("/api/images/references").get_json()
    assert listed["total"] == 1

    assert client.delete(f"/api/images/references/{name}").status_code == 200
    assert not (ref_dir / name).exists()


def test_reference_upload_rejects_non_image(client, monkeypatch, tmp_path):
    monkeypatch.setattr(image_service, "get_image_reference_dir", lambda: str(tmp_path / "r"))
    response = client.post(
        "/api/images/references",
        data={"file": (io_bytes(b"not-an-image"), "ref.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def io_bytes(payload: bytes):
    from io import BytesIO

    return BytesIO(payload)


def test_retry_route_missing_entry_returns_400(client):
    response = client.post("/api/images/history/missing/retry")
    assert response.status_code == 400
    assert response.get_json()["reason"] == "not_found"


def test_retry_reuses_history_params(monkeypatch, history_path):
    image_service.append_image_history({
        "id": "h3",
        "created_at": "2026-10-25T10:00:00+08:00",
        "mode": "img2img",
        "status": "failed",
        "prompt": "a cat",
        "style": "realistic",
        "size": "1024x1024",
        "strength": 0.7,
        "count": 1,
        "reference_images": ["/static/uploads/image_lab/references/a.png"],
        "results": [],
    })
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"success": True, "images": []}

    monkeypatch.setattr(image_service, "generate_image", fake_generate)
    monkeypatch.setattr(image_service, "load_reference_bytes", lambda source: b"img")

    result = image_service.retry_image_generation("h3")
    assert result["success"] is True
    assert captured["mode"] == "img2img"
    assert captured["style"] == "realistic"
    assert captured["strength"] == 0.7
    assert captured["retry_of"] == "h3"


def test_generate_tool_adapter(monkeypatch, history_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", _config)
    monkeypatch.setattr(
        image_service, "call_image_generation", lambda *a, **k: "data:image/png;base64,DDDD"
    )
    monkeypatch.setattr(
        image_service,
        "_save_generated_image",
        lambda data, prefix="": ("/static/uploads/image_lab/lab_2.png", "/tmp/lab_2.png"),
    )
    result = image_service.generate_image_tool(
        {"prompt": "a cat", "style": "pixel", "size": "512x512"}, {"session_id": "s1"}
    )
    assert result["success"] is True
    entry = json.loads(history_path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["source"] == "agent"
    assert entry["session_id"] == "s1"
    assert entry["style"] == "pixel"


# ---------------------------------------------------------------------------
# 生图实验室：模板
# ---------------------------------------------------------------------------


def test_builtin_templates_listed(templates_path):
    result = image_service.list_image_lab_templates()
    ids = [item["id"] for item in result["templates"]]
    assert "builtin:style_restyle" in ids
    assert result["builtin_count"] == len(image_service.BUILTIN_IMAGE_LAB_TEMPLATES)
    for item in result["templates"]:
        assert item["builtin"] is True
        assert 0.0 <= float(item["strength"]) <= 1.0
        assert image_service.normalize_image_size(item["size"]) == item["size"]
        assert 1 <= int(item["count"]) <= image_service._MAX_IMAGES_PER_REQUEST


def test_template_crud(templates_path):
    created = image_service.save_image_lab_template({
        "name": "我的模板",
        "prompt": "a cute cat portrait",
        "style": "anime",
        "size": "768x768",
        "count": 2,
    })
    assert created["id"].startswith("tpl_")
    assert created["builtin"] is False
    saved = json.loads(templates_path.read_text(encoding="utf-8"))["templates"]
    assert saved[0]["name"] == "我的模板"

    listed = image_service.list_image_lab_templates()
    assert listed["total"] == len(image_service.BUILTIN_IMAGE_LAB_TEMPLATES) + 1

    updated = image_service.update_image_lab_template(created["id"], {"style": "ink"})
    assert updated["style"] == "ink"
    assert updated["prompt"] == "a cute cat portrait"

    assert image_service.delete_image_lab_template(created["id"]) is True
    assert image_service.get_image_lab_template(created["id"]) is None
    assert image_service.delete_image_lab_template(created["id"]) is False


def test_template_validation_and_builtin_protection(templates_path):
    with pytest.raises(ValueError):
        image_service.save_image_lab_template({"name": "", "prompt": "x"})
    with pytest.raises(ValueError):
        image_service.save_image_lab_template({"name": "n", "prompt": ""})
    with pytest.raises(ValueError):
        image_service.save_image_lab_template(
            {"name": "n", "prompt": "x", "mode": "img2img"}
        )
    with pytest.raises(ValueError):
        image_service.delete_image_lab_template("builtin:style_restyle")
    with pytest.raises(ValueError):
        image_service.update_image_lab_template("builtin:style_restyle", {"style": "ink"})


# ---------------------------------------------------------------------------
# 生图实验室：参数组合
# ---------------------------------------------------------------------------


def test_build_compare_groups_cartesian(templates_path):
    base = image_service._lab_base_params({"prompt": "a cat"})
    plan = image_service.build_compare_groups(
        base, {"styles": ["anime", "ink"], "strengths": [0.4, 60]}
    )
    assert plan["total"] == 4
    assert plan["truncated"] is False
    styles = [group["params"]["style"] for group in plan["groups"]]
    strengths = [group["params"]["strength"] for group in plan["groups"]]
    assert styles == ["anime", "anime", "ink", "ink"]
    assert strengths == [0.4, 0.6, 0.4, 0.6]
    assert all(group["status"] == "pending" for group in plan["groups"])
    assert plan["groups"][0]["label"]


def test_build_compare_groups_truncates_and_accepts_explicit(templates_path):
    base = image_service._lab_base_params({"prompt": "a cat"})
    plan = image_service.build_compare_groups(
        base, {"styles": ["anime", "ink", "pixel"], "sizes": ["512x512", "768x768", "1024x1024"]}
    )
    assert plan["total"] == image_service._MAX_COMPARE_GROUPS
    assert plan["truncated"] is True

    explicit = image_service.build_compare_groups(
        base,
        {},
        explicit=[{"style": "ink", "count": 2}, {"size": "512x512", "negative_prompt": "blur"}],
    )
    assert explicit["total"] == 2
    assert explicit["groups"][0]["params"]["count"] == 2
    assert explicit["groups"][1]["params"]["size"] == "512x512"
    assert explicit["groups"][1]["params"]["negative_prompt"] == "blur"


def test_lab_base_params_uses_template(templates_path):
    base = image_service._lab_base_params({"template_id": "builtin:outfit_swap"})
    assert base["mode"] == "img2img"
    assert base["style"] == "none"
    assert base["strength"] == pytest.approx(0.7)
    override = image_service._lab_base_params({
        "template_id": "builtin:outfit_swap",
        "style": "realistic",
        "strength": 0.3,
        "reference_images": ["/static/uploads/image_lab/references/a.png"],
    })
    assert override["style"] == "realistic"
    assert override["strength"] == pytest.approx(0.3)

    with pytest.raises(ValueError):
        image_service._lab_base_params({"template_id": "missing", "prompt": "x"})


# ---------------------------------------------------------------------------
# 生图实验室：任务队列
# ---------------------------------------------------------------------------


def _wait_task(task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = image_service.get_image_lab_task(task_id)
        if task and task["status"] in ("succeeded", "partial", "failed", "canceled"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {timeout}s 内完成")


def test_lab_task_reports_groups_and_progress(monkeypatch, history_path, templates_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", lambda: _config())
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "images": [{"url": "/static/uploads/image_lab/x.png"}],
            "history_id": "h1",
            "elapsed_ms": 7,
        }

    monkeypatch.setattr(image_service, "generate_image", fake_generate)
    image_service.clear_image_lab_tasks()

    submitted = image_service.submit_image_lab_task({
        "prompt": "a cat",
        "variations": {"styles": ["anime", "ink"]},
    })
    assert submitted["success"] is True
    task = _wait_task(submitted["task"]["id"])
    assert task["status"] == "succeeded"
    assert task["kind"] == "compare"
    assert [group["status"] for group in task["groups"]] == ["succeeded", "succeeded"]
    assert task["progress"]["percent"] == 100.0
    assert task["image_count"] == 2
    assert len(calls) == 2
    assert calls[0]["source"] == "web_lab"


def test_lab_task_quota_and_validation(monkeypatch, history_path, templates_path):
    image_service.clear_image_lab_tasks()
    monkeypatch.setattr(image_service, "check_image_quota", lambda count=1, now=None: {
        "allowed": False,
        "limit": 1,
        "used": 1,
        "remaining": 0,
        "requested": count,
        "reason": "quota_exceeded",
    })
    rejected = image_service.submit_image_lab_task({"prompt": "a cat"})
    assert rejected["success"] is False
    assert rejected["reason"] == "quota_exceeded"

    monkeypatch.undo()
    with pytest.raises(ValueError):
        image_service._lab_base_params({"prompt": "", "template_id": ""})


def test_lab_task_cancel(monkeypatch, history_path, templates_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", lambda: _config())
    started = threading.Event()
    release = threading.Event()

    def slow_generate(prompt, **kwargs):
        started.set()
        release.wait(3)
        return {"success": True, "images": [], "history_id": "h", "elapsed_ms": 1}

    monkeypatch.setattr(image_service, "generate_image", slow_generate)
    image_service.clear_image_lab_tasks()

    submitted = image_service.submit_image_lab_task({"prompt": "a cat", "variants": [{}, {}]})
    task_id = submitted["task"]["id"]
    assert started.wait(3)
    assert image_service.cancel_image_lab_task(task_id) is True
    release.set()
    task = _wait_task(task_id)
    assert task["status"] == "canceled"
    assert image_service.cancel_image_lab_task(task_id) is False


def test_lab_task_list_summaries(monkeypatch, history_path, templates_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", lambda: _config())
    monkeypatch.setattr(
        image_service,
        "generate_image",
        lambda prompt, **kwargs: {"success": True, "images": [{"url": "/x.png"}], "history_id": "h"},
    )
    image_service.clear_image_lab_tasks()
    submitted = image_service.submit_image_lab_task({"prompt": "a cat"})
    _wait_task(submitted["task"]["id"])

    listed = image_service.list_image_lab_tasks()
    assert listed["total"] == 1
    summary = listed["tasks"][0]
    assert summary["prompt"] == "a cat"
    assert summary["group_count"] == 1


# ---------------------------------------------------------------------------
# 生图实验室：路由
# ---------------------------------------------------------------------------


def test_template_routes(client, templates_path):
    listed = client.get("/api/images/templates")
    assert listed.status_code == 200
    body = listed.get_json()
    assert body["success"] is True
    assert body["builtin_count"] >= 1

    created = client.post(
        "/api/images/templates",
        json={"name": "路由模板", "prompt": "a dog", "style": "realistic"},
    )
    assert created.status_code == 201
    template_id = created.get_json()["template"]["id"]

    updated = client.put(f"/api/images/templates/{template_id}", json={"style": "pixel"})
    assert updated.status_code == 200
    assert updated.get_json()["template"]["style"] == "pixel"

    assert client.delete(f"/api/images/templates/{template_id}").status_code == 200
    assert client.delete(f"/api/images/templates/{template_id}").status_code == 404
    assert client.delete("/api/images/templates/builtin:style_restyle").status_code == 400


def test_lab_plan_route(client, templates_path):
    response = client.post(
        "/api/images/lab/plan",
        json={"prompt": "a cat", "variations": {"styles": ["anime", "ink"], "counts": [1, 2]}},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 4
    assert body["requested_images"] == 6
    assert body["quota"]["allowed"] is True

    bad = client.post("/api/images/lab/plan", json={"prompt": ""})
    assert bad.status_code == 400


def test_lab_task_routes(client, monkeypatch, history_path, templates_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", lambda: _config())
    monkeypatch.setattr(
        image_service,
        "generate_image",
        lambda prompt, **kwargs: {"success": True, "images": [{"url": "/x.png"}], "history_id": "h"},
    )
    image_service.clear_image_lab_tasks()

    created = client.post("/api/images/lab/tasks", json={"prompt": "a cat"})
    assert created.status_code == 202
    task_id = created.get_json()["task"]["id"]

    detail = client.get(f"/api/images/lab/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.get_json()["task"]["groups"]

    listed = client.get("/api/images/lab/tasks").get_json()
    assert listed["total"] == 1

    missing = client.get("/api/images/lab/tasks/lab_missing")
    assert missing.status_code == 404
    assert client.post("/api/images/lab/tasks/lab_missing/cancel").status_code == 404


# ---------------------------------------------------------------------------
# 前端联调补充：历史派生字段 + variants 单组重试
# ---------------------------------------------------------------------------


def test_history_entries_expose_thumbnail_and_urls(history_path):
    image_service.append_image_history({
        "id": "h9",
        "created_at": "2026-10-25T10:00:00+08:00",
        "mode": "txt2img",
        "status": "partial",
        "prompt": "a cat",
        "results": [
            {"status": "success", "url": "/static/uploads/image_lab/a.png", "path": ""},
            {"status": "failed", "url": "", "path": "", "error": "provider 未返回图片"},
        ],
    })
    listed = image_service.list_image_history()["entries"][0]
    assert listed["thumbnail"] == "/static/uploads/image_lab/a.png"
    assert listed["urls"] == ["/static/uploads/image_lab/a.png"]
    assert listed["image_count"] == 2
    assert listed["success_count"] == 1
    assert listed["failed_count"] == 1

    detail = image_service.get_image_history_entry("h9")
    assert detail["thumbnail"] == "/static/uploads/image_lab/a.png"


def test_lab_task_retry_single_group_via_variants(monkeypatch, history_path, templates_path):
    monkeypatch.setattr(image_service, "get_image_generation_config", lambda: _config())
    captured = []

    def fake_generate(prompt, **kwargs):
        captured.append((prompt, kwargs))
        return {"success": True, "images": [], "history_id": "h"}

    monkeypatch.setattr(image_service, "generate_image", fake_generate)
    image_service.clear_image_lab_tasks()

    submitted = image_service.submit_image_lab_task({
        "variants": [{
            "prompt": "a cat",
            "mode": "img2img",
            "style": "ink",
            "size": "768x768",
            "strength": 0.4,
            "count": 2,
            "reference_images": ["/static/uploads/image_lab/references/a.png"],
        }],
    })
    assert submitted["success"] is True
    task = _wait_task(submitted["task"]["id"])
    group = task["groups"][0]
    assert group["status"] == "succeeded"
    assert group["params"]["style"] == "ink"
    assert group["params"]["count"] == 2
    assert task["kind"] == "generate"
    assert len(captured) == 1
    assert captured[0][0] == "a cat"
    assert captured[0][1]["count"] == 2
    assert captured[0][1]["mode"] == "img2img"


def test_lab_task_variants_without_prompt_rejected(monkeypatch, history_path, templates_path):
    image_service.clear_image_lab_tasks()
    rejected = image_service.submit_image_lab_task({"variants": [{"style": "ink"}]})
    assert rejected["success"] is False
    assert rejected["reason"] == "invalid_parameter"


def test_lab_plan_route_accepts_variants_only(client, templates_path):
    response = client.post(
        "/api/images/lab/plan",
        json={"variants": [{"prompt": "a cat", "style": "ink", "count": 2}]},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1
    assert body["groups"][0]["params"]["count"] == 2
    assert body["requested_images"] == 2
