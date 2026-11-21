"""会话导入（Web 面板）前端静态校验。

只断言模板/脚本里存在导入流程的关键锚点，避免重构时把深链或接口路径改丢。
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


TEMPLATE = _read("nbot/web/templates/index.html")
METHODS = _read("nbot/web/static/js/nbot-methods.js")
CSS = _read("nbot/web/static/css/app.css")


def test_sessions_toolbar_opens_import_dialog():
    assert 'ref="sessionImportArchiveInput"' in TEMPLATE
    assert '@click="openSessionImportDialog"' in TEMPLATE
    assert 'accept=".zip,application/zip"' in TEMPLATE


def test_import_dialog_has_format_archive_and_select_steps():
    for token in (
        "sessionImportDialog.step === 'format'",
        "sessionImportDialog.step === 'archive'",
        "sessionImportDialog.step === 'select'",
        "chooseSessionImportFormat(fmt.id)",
        "scanDeepSeekArchiveFromUrl",
        "commitSessionImport",
        "toggleAllSessionImportConversations",
    ):
        assert token in TEMPLATE, token


def test_methods_define_import_flow():
    for token in (
        "openSessionImportDialog()",
        "loadSessionImportFormats()",
        "handleDeepSeekArchiveFile(event)",
        "scanDeepSeekArchive(form, url)",
        "commitSessionImport()",
        "/api/sessions/import/formats",
        "/api/sessions/import/deepseek/scan",
        "/api/sessions/import/deepseek/import",
        "/api/sessions/import/deepseek/discard",
        "SESSION_IMPORT_BATCH",
    ):
        assert token in METHODS, token


def test_stylesheet_covers_import_dialog():
    for token in (".session-import-modal", ".session-import-list", ".session-import-item"):
        assert token in CSS, token
