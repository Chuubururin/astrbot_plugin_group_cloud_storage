<div align="center">

# astrbot_plugin_group_cloud_storage

![logo](logo.png)

_群云存储管理器_

[![License](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/Soulter/AstrBot)

</div>

QQ 群原生云存储（群文件 / 群相册 / 精华消息）统一管理插件，经 OneBot 协议对接 NapCat 等协议端。支持多账号，云端为真源，本地仅维护可重建的元数据索引，文件内容不落盘（例外：开启下载服务后，被拉取的文件会按需暂存到插件私有临时目录，用于避免同一文件被反复下载；该目录随插件重载整体清空，并受 `download_cache_max_mb` / `download_cache_ttl_hours` 约束）。

> [!NOTE]
> 第三方工具，请遵守《QQ 用户协议》，按"现状"使用，风险自负。

## 功能

- **群文件**：浏览、搜索（文件名/后缀/上传者/群号/群名/标签）、上传（本地 / URL 链接 / 由网盘 / 由相册 / 由精华）、下载与直链（HTTP / SFTP / SMB）、改名 / 移动 / 删除 / 新建一级文件夹、批量操作、标签；超过 95MB 的文件自动分卷压缩上传；13 类扩展名分类筛选；派生状态筛选（在网盘 / 在相册 / 在精华消息 / 未下载，按交叉存在性判定）。
- **群相册**：媒体缩略图浏览、图库查看；图片上传；自定义标签筛选。相册**视频**上传当前不可用——协议端相册接口只收图片，插件在动 ffmpeg 之前就以 UNSUPPORTED 终态拒绝，长视频请存群文件（见 [入库与组合存储](docs/入库与组合存储.md)）。
- **精华消息**：长文本保存（自动按 4000 字分段成多条精华）、查看、全文重建、删除。
- **网盘（OpenList）**：群文件 ↔ 网盘双向转存，目录浏览、批量标记、深度索引；支持按大小限制与路径模板归档。
- **任务**：全部队列操作的记录与进度，支持暂停 / 继续 / 中断。
- **群组**：多账号群列表聚合（在线账号的群自动聚合展示，账号离线即隐藏）、自动编号、批量改名备注、账号筛选。
- **配置**：面板内可视化配置，敏感项脱敏显示。

网页面板以 AstrBot 插件页面（Page）形式嵌入 WebUI。

## 快速开始

1. 将插件放入 AstrBot `data/plugins/`（勿用符号链接），重启 AstrBot
2. WebUI → 插件 → 群云存储管理器 → 插件页面
3. 面板内完成基础配置：`managed_groups`（留空 = 管理机器人所在全部群）、`download_token`（启用下载服务时必填）等

## 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/cssync [群号]` | 同步群云存储索引与统计 |
| `/csfiles [群号] [页]` | 文件列表 |
| `/csfile <id> [群号]` | 文件详情 + 下载链接 |
| `/cssave [群号] <标题> <正文>` | 保存文本为精华消息（自动分段） |
| `/csfetch [群号] <URL> [文件名]` | 拉取外部文件到群文件（http/https/sftp/smb） |
| `/csarchive [群号] <id或名> [--force]` | 转存文件到 OpenList 网盘 |
| `/csbridge status\|cancel\|retry [任务ID]` | 网盘桥接任务管理 |
| `/cshelp` | 帮助 |

## 主要配置

分类与 [_conf_schema.json](_conf_schema.json) 的 `group` 字段一致（页面「配置」Tab 按分类展示）：

| 分类 | 配置项 | 默认 | 说明 |
| --- | --- | --- | --- |
| 基础设置 | `managed_groups` | `[]` | 受管群白名单；留空 = 全部可管理 |
| 基础设置 | `global_admin_qqs` | `[]` | 全局管理员 QQ 号 |
| 基础设置 | `request_interval` | `1.0` | QQ 接口调用间隔（秒），防风控 |
| 基础设置 | `auto_scan_interval_hours` | `6` | 定时与云端对账周期（小时），0 = 关闭 |
| 自动保存与分卷 | `fetch_max_size` | `2GB` | 外部链接导入单文件上限 |
| 下载服务 | `download_server_enabled` | `false` | 本机下载服务（HTTP/SFTP/SMB 直链） |
| 下载服务 | `download_cache_max_mb` | `1024` | 下载缓存容量上限（MB，0 = 不限） |
| 网盘归档 | `openlist_enabled` | `false` | OpenList 网盘桥接 |
| 高级选项 | `volume_threshold` | `95MB` | 分卷上传阈值 |

完整配置见 [_conf_schema.json](_conf_schema.json) 与 [docs/配置项总表.md](docs/配置项总表.md)。

## 开发与测试

- **版本号**唯一定义在 [`metadata.yaml`](metadata.yaml) 的 `version`；发布流程
  （dev 提交 `chore(release): vX.Y.Z` → 自动 promote 到 main → 在 main 打 tag）见
  [docs/版本与发布.md](docs/版本与发布.md)。
- **分支模型**：`dev` = 日常开发分支，可直接 push（push 时自动跑 CI）；
  `main` = 稳定分支，只能经 dev→main PR 合入，且五项门禁检查
  （`lint` / `python-tests` / `frontend-tests` / `contract-checks` / `typecheck`）
  全绿才允许合并（promote workflow 逐名轮询这五项；GitHub 分支保护侧亦应把
  `typecheck` 列为 required check）。
- **本地自检**（与 CI 同口径，提交前过一遍）：

  ```bash
  REQUIRE_NO_SKIP=1 python3 -m pytest tests/ -q          # 全量（CI 串行口径，skip 必须为 0）
  python3 -m pytest tests/ -q -n auto --dist loadfile    # CI 并行口径（需 pytest-xdist）
  ruff check . --output-format=github                    # 与 CI 同版本 ruff==0.16.6
  node --test pages/storage-ng/testing/unit/*.test.mjs   # 前端单测
  python3 tools/check_import_contracts.py                # 分层契约 C1-C4
  python3 tools/check_doc_drift.py .                     # 配置 schema / 路由表 / 前端 API 漂移
  python3 -m pyright                                     # 静态类型门禁（同 CI typecheck job）
  ```

  完整自检清单（含体积门禁与 `comm` 用例对账口径）见
  [docs/版本与发布.md](docs/版本与发布.md)。

## 文档

设计与接口详情见 [docs/](docs/)：需求简报、架构总览、接口契约、前端架构、入库与组合存储、桥接与网盘设计、数据与编码规范、外部对接指南。版本管理与发布流程见[版本与发布](docs/版本与发布.md)。

## 许可

[AGPL v3](LICENSE)
