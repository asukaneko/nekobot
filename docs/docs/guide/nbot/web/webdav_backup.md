# webdav_backup - WebDAV 配置备份与同步

## 概述

`webdav_backup.py` 实现基于 WebDAV 协议的配置文件多端备份与同步。用户只需填写一个 WebDAV 根地址（兼容坚果云、Nextcloud、自建 WebDAV 等主流服务），模块会在该地址下自动创建 `nekobot/` 目录，并将加密后的 `config.nbotcfg` 配置包上传或下载，实现跨设备配置同步。

> v3.0.7 起新增**角色立绘文件**的备份/同步支持：勾选 `include_portraits` 后，立绘会随配置包一起上传到 `nekobot/portraits/` 子目录，同步时自动恢复到本地 `static/uploads/portraits/`。

## 设计思路

### 为什么需要 WebDAV 备份

- **跨设备同步**：在家用机、服务器、笔记本之间同步同一份 NekoBot 配置
- **可观测**：配置包是加密 JSON，可以预览导入/跳过的字段
- **容灾**：避免单点设备故障导致配置丢失
- **生态复用**：用户无需安装新客户端，复用已有的网盘账户（坚果云 / Nextcloud / 自建 WebDAV）

### 文件结构

```text
{webdav_root}/
  └── nekobot/
      ├── config.nbotcfg        # 加密配置包（JSON）
      └── portraits/            # 可选：立绘子目录
          ├── alice.png
          ├── bob.jpg
          └── portraits-manifest.json   # 立绘清单（PROPFIND 不可用时回退使用）
```

`config.nbotcfg` 是 `config_transfer.encrypt_bundle()` 的产物，由用户自行设置的 `encryption_password` 加密，与 WebDAV 账号密码相互独立。

### 配置包格式兼容

读取远程包 / 导入本地文件时，服务端**全程使用二进制读取**，先按文件头嗅探容器再解码，绝不把二进制密文当 UTF-8 文本读（历史 bug 见 issue #16：`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xba in position 10`）。`config_transfer.load_bundle_bytes()` 支持的布局：

| 文件头 | 容器 | 处理方式 |
|--------|------|----------|
| `PK\x03\x04` | ZIP | 取出 `config.nbotcfg` 成员后递归解析（含立绘包） |
| `\x1f\x8b` | GZIP | 解压后递归解析 |
| `{` / `{`+BOM | JSON 文本 | 按 UTF-8(BOM) / UTF-16(BOM) / GB18030 解码后解密 |
| 其他 | 裸二进制 | 前 16 字节为 KDF salt，其余为 Fernet token（原始字节或 Base64） |

此外还兼容「salt 以原始字节内嵌在 JSON 字符串里」的伪 JSON：这类文件从 `{"salt": "` 之后就是随机二进制，只能按字节定位 `salt` / `payload` 字段再解密。

几个边界处理：

- 容器魔数命中但解包失败（如 salt 恰好以 `\x1f\x8b` 开头）时，会回退按「salt + token」布局再试一次；
- 截断 / 损坏的 GZIP（`EOFError`、`zlib.error`、`BadGzipFile`）统一转成 `ConfigTransferError`，返回可读错误而不是 500；
- `salt` 写成整数数组时，越界值按非法处理，不会抛 `ValueError`；
- 嵌套容器（ZIP 套 ZIP、GZIP 包裹 ZIP）里的 `portraits/` 立绘会被一并收集并恢复，导入路由用 `load_bundle_bytes_with_portraits()` 取回 `(bundle, portraits)`。

无法识别时的报错会带上容器类型、文件大小和头 16 字节十六进制，便于排查；密码错误仍返回 `密码错误或配置包已损坏`。

### 兼容性策略

不同 WebDAV 服务商对 `PROPFIND` / `HEAD` 的支持差异较大（典型如坚果云对根目录 `HEAD` 返回 403），模块通过**探测 + 回退**策略保证在大多数服务商下都能工作：

| 操作 | 首选 | 回退 |
|------|------|------|
| 探测文件存在 | `PROPFIND` | `HEAD` |
| 探测目录存在 | `PROPFIND` | `MKCOL`（已存在则返回 405） |
| 列出立绘文件 | `PROPFIND` | 读取 `portraits-manifest.json` |
| 读元信息（大小/时间） | `PROPFIND` (XML 解析) | `HEAD`（部分字段缺失） |

只有 `401`（认证失败）才视为真正的错误；`403` / `404` 都会继续尝试后续步骤。

## 安全机制

### 加密

- 配置包在上传前使用 `config_transfer.encrypt_bundle()` 加密，加密密码为 `encryption_password`
- 加密密码与 WebDAV 账号密码**互不依赖**：即使 WebDAV 账号泄露，攻击者仍需破解加密密码
- 同步时若 `encryption_password` 不一致，模块会抛 `ConfigTransferError`

### 密码脱敏

- `get_config()` 返回时对 `password` / `encryption_password` 字段调用 `_mask()` 脱敏（如 `mypassword` → `my********rd`）
- 前端提交保存时，掩码字符串（`"*"`）会被识别为「不修改」，避免意外覆盖
- 真正清空密码需要显式传 `clear_password=true` / `clear_encryption_password=true`

### 超时与重试

- 默认请求超时 30 秒
- 单文件 > 50MB 时超时翻倍为 60 秒
- 不做自动重试：失败立即抛出 `WebDAVBackupError`，由前端展示错误信息

## 核心 API

### get_config(server)

读取 WebDAV 备份配置（密码字段会被脱敏）。

```python
from nbot.web.webdav_backup import get_config

cfg = get_config(server)
# {
#   "enabled": True,
#   "url": "https://dav.jianguoyun.com/dav/",
#   "resolved_file_url": "https://dav.jianguoyun.com/dav/nekobot/config.nbotcfg",
#   "folder": "nekobot",
#   "filename": "config.nbotcfg",
#   "username": "user@example.com",
#   "password": "my********rd",
#   "encryption_password": "en********pt",
#   "has_password": True,
#   "has_encryption_password": True,
#   "last_backup_at": "2026-07-05T19:11:18",
#   "last_sync_at": "",
#   "last_error": "",
#   "last_file_size": 12345,
#   "last_modified": "Sat, 05 Jul 2026 11:11:18 GMT",
# }
```

### save_config(server, payload)

保存 WebDAV 备份配置到 `server.settings["webdav_backup"]`。

- 密码字段为空字符串或形如 `"ab****cd"` 时视为不修改
- 想清空密码需额外传 `clear_password=true` / `clear_encryption_password=true`
- 保存后会重置 `last_backup_at` / `last_sync_at` / `last_error` 等状态字段

### test_connection(server, config_override=None)

测试 WebDAV 连接。流程：

1. 校验根地址格式（必须 `http://` 或 `https://` 开头）
2. 对根地址执行 `HEAD` 请求验证连通性（`403` 不视为失败，因为某些服务器不允许 `HEAD` 根目录）
3. 检查 / 创建 `nekobot/` 文件夹（`PROPFIND` 探测 + `MKCOL` 兜底）
4. 对配置文件执行 `HEAD` / `PROPFIND`，返回存在性、大小、最近修改时间

```python
from nbot.web.webdav_backup import test_connection

result = test_connection(server, config_override={
    "url": "https://dav.jianguoyun.com/dav/",
    "username": "user",
    "password": "secret",
})
# {
#   "ok": True,
#   "status_code": 200,
#   "exists": True,
#   "message": "连接成功，远程配置文件已存在",
#   "last_modified": "Sat, 05 Jul 2026 11:11:18 GMT",
#   "content_length": 12345,
#   "folder_exists": True,
#   "folder_created": False,
#   "resolved_file_url": "https://dav.jianguoyun.com/dav/nekobot/config.nbotcfg",
# }
```

### upload_backup(server, password_override=None, include_portraits=False)

构建加密 `.nbotcfg` 配置包并上传到 WebDAV。

- `password_override` 优先于配置中保存的 `encryption_password`
- `include_portraits=True` 时同时上传立绘文件到 `nekobot/portraits/` 子目录
- 上传成功后更新 `last_backup_at` / `last_file_size` / `last_modified`

```python
from nbot.web.webdav_backup import upload_backup

result = upload_backup(server, include_portraits=True)
# {
#   "ok": True,
#   "size": 12345,
#   "uploaded_at": "2026-07-05T19:11:18",
#   "status_code": 201,
#   "last_modified": "Sat, 05 Jul 2026 11:11:18 GMT",
#   "file_url": "https://dav.jianguoyun.com/dav/nekobot/config.nbotcfg",
#   "portraits": {
#     "ok": True, "uploaded": 3, "skipped": 0, "failed": 0, "total": 3,
#   },
# }
```

### pull_backup(server, password_override=None, include_portraits=False)

从 WebDAV 拉取 `.nbotcfg` 文件，解密并应用到本地。

- 拉取成功后调用 `config_transfer.apply_bundle(server, bundle, overwrite=True)` 应用配置
- `include_portraits=True` 时同时从 `nekobot/portraits/` 拉取立绘文件并恢复
- 立绘下载到 `nbot/web/static/uploads/portraits/` 下，覆盖同名文件

```python
from nbot.web.webdav_backup import pull_backup

result = pull_backup(server, include_portraits=True)
# {
#   "ok": True,
#   "size": 12345,
#   "synced_at": "2026-07-05T19:11:18",
#   "imported": ["characters", "personality", "world_books"],
#   "skipped": [],
#   "exported_at": "2026-07-05T18:00:00",
#   "file_url": "...",
#   "portraits": {"ok": True, "downloaded": 3, "failed": 0, "total": 3},
# }
```

### get_remote_info(server)

查询远程备份文件的元信息。优先用 `PROPFIND`（可获取真实 `Content-Length` 和 `Last-Modified`），失败时回退到 `HEAD`。

```python
from nbot.web.webdav_backup import get_remote_info

info = get_remote_info(server)
# {
#   "ok": True, "exists": True, "size": 12345,
#   "last_modified": "Sat, 05 Jul 2026 11:11:18 GMT",
#   "status_code": 207,
#   "message": "",
#   "file_url": "...",
# }
```

## 路由注册

在 `nbot/web/routes/__init__.py` 中导入并注册 WebDAV 备份路由：

```python
from nbot.web.routes.webdav_backup import register_webdav_backup_routes

register_webdav_backup_routes(app, server)
```

注册后暴露以下 Flask 端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`  | `/api/webdav/config` | 读取配置（密码脱敏） |
| `PUT`  | `/api/webdav/config` | 保存配置 |
| `POST` | `/api/webdav/test` | 测试连接（可临时覆盖配置） |
| `GET`  | `/api/webdav/info` | 查询远程备份文件元信息 |
| `POST` | `/api/webdav/backup` | 执行备份（请求体支持 `password` / `include_portraits`） |
| `POST` | `/api/webdav/sync` | 执行同步（请求体支持 `password` / `include_portraits`） |

`/api/webdav/test` 的请求体可临时传入 `url` / `username` / `password`，仅做连接测试，**不持久化**。

## 立绘文件处理

`include_portraits=True` 时，备份/同步会同时处理 `static/uploads/portraits/` 下的立绘。

### 备份流程

1. 通过 `config_transfer._collect_portrait_paths()` 收集当前配置中**被引用**的立绘文件
2. 确保 `nekobot/portraits/` 子目录存在（递归 MKCOL 父目录）
3. 逐个 `PUT` 上传立绘
4. 上传 `portraits-manifest.json` 清单文件（备份 PROPFIND 不可用时回退使用）

### 同步流程

1. 优先 `PROPFIND` 列出 `nekobot/portraits/` 下的所有文件
2. 回退到读取 `portraits-manifest.json` 清单
3. 逐个 `GET` 下载到 `nbot/web/static/uploads/portraits/`，覆盖同名文件
4. 失败的立绘不影响配置导入，仅记录到返回结果的 `portraits.failed` 与 `portraits.errors`

### 结果结构

```python
{
  "ok": True,
  "uploaded": 3,      # 备份时：成功上传数量
  "downloaded": 3,    # 同步时：成功下载数量
  "skipped": 0,       # 本地文件不存在的跳过数
  "failed": 0,        # 失败数量
  "total": 3,         # 总数
  "errors": [],       # 最多 10 条错误明细
}
```

立绘处理失败**不会**中断主流程：即使所有立绘都失败，配置包本身仍能正常备份/同步。

## 状态字段

`server.settings["webdav_backup"]` 在运行时会更新以下状态字段，可用于前端展示「上次备份时间」「最近错误」等：

| 字段 | 类型 | 说明 |
|------|------|------|
| `last_backup_at` | ISO 8601 字符串 | 上次成功备份时间 |
| `last_sync_at` | ISO 8601 字符串 | 上次成功同步时间 |
| `last_error` | 字符串 | 最近一次失败的错误信息（成功时清空） |
| `last_file_size` | 整数 | 最近一次成功上传/下载的字节数 |
| `last_modified` | HTTP 头字符串 | 远程文件的 `Last-Modified` |

## 错误处理

模块定义 `WebDAVBackupError(RuntimeError)`，所有用户可见的错误都通过该异常抛出，路由层捕获后返回 `400 + {"success": False, "error": "..."}`。

常见错误：

| 错误信息 | 原因 |
|----------|------|
| `未设置加密密码` | 配置中无 `encryption_password` 且请求体未提供 |
| `认证失败 (HTTP 401)` | WebDAV 用户名 / 密码错误 |
| `无权限创建文件夹 (HTTP 403)` | 账号对根目录无写权限 |
| `父目录不存在，请检查 WebDAV 根地址是否正确` | 根地址路径不合法（HTTP 409） |
| `远程备份文件不存在，请先执行备份` | 拉取时 `GET` 返回 404 |
| `远程备份文件不是有效的 JSON` | 远端文件不是 `config_transfer` 格式 |
| `上传超时 / 拉取超时` | 网络问题或配置包过大 |
