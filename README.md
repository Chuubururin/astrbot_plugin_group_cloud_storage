<div align="center">

# astrbot_plugin_group_cloud_storage

![logo](logo.png)

_群云存储管理器_

[![License](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/Soulter/AstrBot)

</div>

QQ 群原生云存储（群文件 / 群相册 / 精华消息）统一管理插件，经 OneBot 协议对接 NapCat 等协议端。支持多账号，云端为真源，本地仅维护可重建的元数据索引，文件内容不落盘。

> [!NOTE]
> 第三方工具，请遵守《QQ 用户协议》，按"现状"使用，风险自负。

## 功能

- **群文件**：浏览、搜索（文件名/后缀/上传者/群号/群名/标签）、上传（本地 / URL 链接 / 由网盘 / 由相册 / 由精华）、下载与直链（HTTP / SFTP / SMB）、改名 / 移动 / 删除 / 新建一级文件夹、批量操作、标签；超过 95MB 的文件自动分卷压缩上传；13 类扩展名分类筛选；派生状态筛选（在网盘 / 在相册 / 在精华消息 / 未下载，按交叉存在性判定）。
- **群相册**：媒体缩略图浏览、图库查看；图片上传、长视频自动分段导入（按 QQ 相册 599 秒上限切分）；自定义标签筛选。
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
| `/csfetch [群号] <URL> [文件名]` | 拉取外部文件到群文件（http/https/sftp） |
| `/csarchive [群号] <id或名> [--force]` | 转存文件到 OpenList 网盘 |
| `/csbridge status\|cancel\|retry [任务ID]` | 网盘桥接任务管理 |
| `/cshelp` | 帮助 |

## 主要配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `managed_groups` | `[]` | 受管群白名单；留空 = 全部可管理 |
| `global_admin_qqs` | `[]` | 全局管理员 QQ 号 |
| `request_interval` | `1.0` | QQ 接口调用间隔（秒），防风控 |
| `auto_scan_interval_hours` | `6` | 定时与云端对账周期（小时），0 = 关闭 |
| `volume_threshold` | `95MB` | 分卷上传阈值 |
| `fetch_max_size` | `2GB` | 外部链接导入单文件上限 |
| `download_server_enabled` | `false` | 本机下载服务（HTTP/SFTP/SMB 直链） |
| `openlist_enabled` | `false` | OpenList 网盘桥接 |

完整配置见 [_conf_schema.json](_conf_schema.json) 与 [docs/配置项总表.md](docs/配置项总表.md)。

## 文档

设计与接口详情见 [docs/](docs/)：需求简报、架构总览、接口契约、前端架构、入库与组合存储、桥接与网盘设计、数据与编码规范、外部对接指南。版本管理与发布流程见[版本与发布](docs/版本与发布.md)。

## 许可

[AGPL v3](LICENSE)
