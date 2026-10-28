"""Tests for MemoryFS — truncation and basic CRUD."""

import tempfile

from nbot.memory.fs import (
    _MAX_DIARY_ENTRIES,
    _MAX_MEMORY_FILE_CHARS,
    _MAX_PLOT_ENTRIES,
    MemoryFS,
    _truncate_entries,
)


def test_write_and_read():
    """Basic write/read roundtrip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write("test.md", content="hello world", title="Test")
        mf = mfs.read("test.md")
        assert mf is not None
        assert mf.content == "hello world"
        assert mf.title == "Test"


def test_append_merges_content():
    """Append should concatenate with separator."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write("log.md", content="first", append=False)
        mfs.write("log.md", content="second", append=True)
        mf = mfs.read("log.md")
        assert "first" in mf.content
        assert "second" in mf.content


def test_diary_truncation():
    """Diary entries should be truncated to _MAX_DIARY_ENTRIES."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        path = mfs.path_diary_daily("char-1")
        # Write more entries than the limit
        for i in range(_MAX_DIARY_ENTRIES + 10):
            mfs.write(path, character_id="char-1", content=f"entry-{i}", append=True)
        mf = mfs.read(path)
        entries = mf.content.split("\n\n")
        assert len(entries) <= _MAX_DIARY_ENTRIES
        # Latest entries should survive
        assert any("entry-" in e for e in entries)


def test_plot_truncation():
    """Plot entries should be truncated to _MAX_PLOT_ENTRIES."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        path = mfs.path_plot("char-1", "conv-1")
        for i in range(_MAX_PLOT_ENTRIES + 5):
            mfs.write(path, character_id="char-1", content=f"plot-{i}", append=True)
        mf = mfs.read(path)
        entries = mf.content.split("\n\n")
        assert len(entries) <= _MAX_PLOT_ENTRIES


def test_user_file_truncation():
    """User files should be truncated to _MAX_MEMORY_FILE_CHARS."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        path = mfs.path_user("char-1", "user-1")
        long_content = "x" * (_MAX_MEMORY_FILE_CHARS + 1000)
        mfs.write(path, character_id="char-1", target_id="user-1", content=long_content, append=True)
        mf = mfs.read(path)
        assert len(mf.content) <= _MAX_MEMORY_FILE_CHARS


def test_truncate_entries_diary():
    """_truncate_entries should keep last N diary entries."""
    entries = [f"entry-{i}" for i in range(50)]
    content = "\n\n".join(entries)
    result = _truncate_entries(content, "characters/c/diary/daily.md")
    result_entries = result.split("\n\n")
    assert len(result_entries) == _MAX_DIARY_ENTRIES
    assert result_entries[-1] == "entry-49"


def test_truncate_entries_plot():
    """_truncate_entries should keep last N plot entries."""
    entries = [f"plot-{i}" for i in range(60)]
    content = "\n\n".join(entries)
    result = _truncate_entries(content, "characters/c/plot/conv.md")
    result_entries = result.split("\n\n")
    assert len(result_entries) == _MAX_PLOT_ENTRIES


def test_list_for_character():
    """list_for_character should return files for that character only."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write(mfs.path_user("char-a", "u1"), character_id="char-a", content="a")
        mfs.write(mfs.path_user("char-b", "u1"), character_id="char-b", content="b")
        files = mfs.list_for_character("char-a")
        assert len(files) == 1
        assert files[0].character_id == "char-a"


def test_delete():
    """delete should remove the file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write("del.md", content="to delete")
        assert mfs.read("del.md") is not None
        assert mfs.delete("del.md") is True
        assert mfs.read("del.md") is None


def test_version_increments():
    """Version should increment on each write."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write("v.md", content="v1")
        mfs.write("v.md", content="v2")
        mf = mfs.read("v.md")
        assert mf.version == 2


def test_prompt_context_uses_structured_memory_without_legacy_daily():
    with tempfile.TemporaryDirectory() as tmpdir:
        mfs = MemoryFS(data_dir=tmpdir)
        mfs.write(
            mfs.path_user_persona("char-1", "user-1"),
            character_id="char-1",
            target_id="user-1",
            title="用户人格记忆",
            content="用户偏好克制安慰。",
            summary="用户偏好克制安慰",
        )
        mfs.write(
            mfs.path_character_persona("char-1", "user-1"),
            character_id="char-1",
            target_id="user-1",
            title="角色人格记忆",
            content="角色会先安静陪伴。",
            summary="角色会先安静陪伴",
        )
        mfs.write(
            mfs.path_important_events("char-1", "conv-1"),
            character_id="char-1",
            target_id="user-1",
            title="雨夜约定",
            content="用户和角色约定下次继续雨夜谈话。",
            summary="雨夜约定",
        )
        mfs.write(
            mfs.path_recent_digest("char-1", "user-1"),
            character_id="char-1",
            target_id="user-1",
            title="近期对话压缩摘要",
            content="近期围绕安慰方式和下次约定。",
            summary="安慰方式和下次约定",
        )
        mfs.write(
            mfs.path_diary_daily("char-1"),
            character_id="char-1",
            title="旧日记",
            content="用户：这段旧 raw daily 不应被注入。",
        )

        context = mfs.build_prompt_context("char-1", "user-1", "conv-1")

        assert "用户偏好克制安慰" in context
        assert "角色会先安静陪伴" in context
        assert "雨夜约定" in context
        assert "安慰方式和下次约定" in context
        assert "旧 raw daily" not in context
