"""上下文统计前端回归测试

用 Node 跑 ``tests/js/context_stats_harness.js``，直接加载真实的 nbot-methods.js，
验证「空会话详情列表循环闪断」与「消息数 ±1 抖动」不再复现。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "js" / "context_stats_harness.js"
METHODS = (ROOT / "nbot" / "web" / "static" / "js" / "nbot-methods.js").read_text(
    encoding="utf-8"
)

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node 不可用，跳过前端状态机回归")
def test_context_stats_harness_passes():
    result = subprocess.run(
        [NODE, str(HARNESS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "全部通过" in (result.stdout or "")


def test_update_context_stats_has_single_source():
    """updateContextStats 不应再写本地估算值（否则会与服务端值互相覆盖）。"""
    start = METHODS.index("                updateContextStats() {")
    end = METHODS.index("                // 有占比的类别", start)
    block = METHODS[start:end]

    assert "contextMessageCount = contextMessages.length" not in block
    assert "estimateMessageTokens(" not in block
    assert "_contextStatsSignature" in block
    assert "resetContextStats" in block


def test_breakdown_not_cleared_on_empty_session():
    start = METHODS.index("                resetContextStats() {")
    end = METHODS.index("                // 有占比的类别", start)
    block = METHODS[start:end]
    # 只有 resetContextStats（切会话）可以清空 breakdown
    assert block.count("this.contextBreakdown = null;") == 1
    assert "scheduleContextBreakdownRefresh(signature)" in block


def test_rows_filter_empty_categories():
    start = METHODS.index("                contextBreakdownRows() {")
    end = METHODS.index("                toggleContextTooltip()", start)
    block = METHODS[start:end]
    assert ".filter(row => row.tokens > 0 || row.chars > 0)" in block
