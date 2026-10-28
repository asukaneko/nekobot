import os

import pytest
from flask import Flask

from nbot.core import skills_manager
from nbot.core.skills_manager import (
    SkillPathError,
    SkillStorage,
    get_skills_storage_manager,
    resolve_within,
    sanitize_skill_name,
)
from nbot.web.routes.skills_storage import register_skills_storage_routes


def test_sanitize_skill_name_rejects_traversal():
    for bad in ("", ".", "..", "../evil", "a/b", "a\\b", "/abs", "C:\\abs", "x" * 101):
        with pytest.raises(SkillPathError):
            sanitize_skill_name(bad)
    assert sanitize_skill_name(" my-skill ") == "my-skill"
    assert sanitize_skill_name("中文技能") == "中文技能"


def test_resolve_within_rejects_escape(tmp_path):
    base = tmp_path / "skills"
    base.mkdir()
    assert resolve_within(str(base), "scripts/a.py") == str(base / "scripts" / "a.py")
    assert resolve_within(str(base), "scripts\\a.py") == str(base / "scripts" / "a.py")

    for bad in ("../outside.txt", "sub/../../outside.txt", "C:\\Windows\\evil.txt"):
        with pytest.raises(SkillPathError):
            resolve_within(str(base), bad)


def test_skill_storage_rejects_unsafe_names(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_manager, "SKILLS_ROOT", str(tmp_path / "skills"))
    with pytest.raises(SkillPathError):
        SkillStorage("../escape")

    store = SkillStorage("demo")
    with pytest.raises(SkillPathError):
        store.save_script("../evil.py", "print(1)")
    with pytest.raises(SkillPathError):
        store.save_resource("../../evil.txt", "x")

    manager = get_skills_storage_manager()
    assert manager.skill_exists("../escape") is False
    assert manager.delete_skill("../escape") is False


def test_skill_file_routes_keep_paths_inside_skill_dir(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_manager, "SKILLS_ROOT", str(root))

    app = Flask(__name__)
    register_skills_storage_routes(app, server=None)
    client = app.test_client()
    get_skills_storage_manager().create_skill("demo")

    response = client.post(
        "/api/skills/storage/demo/file/sub/note.txt",
        json={"content": "hello"},
    )
    assert response.status_code == 200
    assert (root / "demo" / "sub" / "note.txt").read_text(encoding="utf-8") == "hello"

    escaped = client.post(
        "/api/skills/storage/demo/file/..%2Foutside.txt",
        json={"content": "hacked"},
    )
    assert escaped.status_code in (400, 404)
    assert not (root / "outside.txt").exists()


def test_upload_folder_rejects_traversal(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_manager, "SKILLS_ROOT", str(root))

    class Server:
        skills_config = []

        def _save_data(self, _name):
            pass

    app = Flask(__name__)
    register_skills_storage_routes(app, server=Server())
    client = app.test_client()

    response = client.post(
        "/api/skills/upload-folder",
        data={"folder_name": "../evil"},
    )
    assert response.status_code == 400
    assert not (tmp_path / "evil").exists()

    from io import BytesIO

    response = client.post(
        "/api/skills/upload-folder",
        data={
            "folder_name": "safe-skill",
            "files": (BytesIO(b"data"), "../../escape.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert not (tmp_path / "escape.txt").exists()
    assert os.path.isfile(root / "safe-skill" / "SKILL.md")
