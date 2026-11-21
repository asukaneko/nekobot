"""会话导入格式注册表。

Web 面板的「导入会话」弹窗按 :data:`SESSION_IMPORT_FORMATS` 的顺序渲染格式列表，
新增格式只需在这里追加一项，并在弹窗里接上对应的「来源选择」步骤
（``nekobot`` 是单文件 JSON，``deepseek`` 是 ZIP + 勾选会话）。
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SessionImportFormat:
    """一种可导入的会话归档格式。

    Attributes:
        id: 稳定的格式标识，用于接口参数与日志，不随界面文案变化。
        label: 格式名称。
        description: 格式简介。
        needs_archive_selection: 是否先选定一个归档（ZIP）再勾选其中的会话。
    """

    id: str
    label: str
    description: str
    needs_archive_selection: bool


SESSION_IMPORT_FORMATS: tuple[SessionImportFormat, ...] = (
    SessionImportFormat(
        id="nekobot",
        label="nekobot 格式",
        description="会话 JSON 导出文件",
        needs_archive_selection=False,
    ),
    SessionImportFormat(
        id="deepseek",
        label="DeepSeek 官方格式",
        description="官方数据导出 ZIP，可用链接或本地文件",
        needs_archive_selection=True,
    ),
)


def get_session_import_format(format_id: str) -> SessionImportFormat | None:
    """按 id 查找格式；未知 id 返回 ``None``。"""
    target = str(format_id or "").strip()
    for item in SESSION_IMPORT_FORMATS:
        if item.id == target:
            return item
    return None


def list_session_import_formats() -> list[dict]:
    """返回可直接 JSON 化的格式列表（顺序与界面展示顺序一致）。"""
    return [asdict(item) for item in SESSION_IMPORT_FORMATS]
