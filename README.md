<div align="center">

# astrbot_plugin_group_cloud_storage

![logo](logo.png)

_群云存储管理器_

[![License](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)

</div>

QQ 群原生云存储（群文件 / 群相册 / 群精华）的统一管理插件，通过 OneBot 协议对接 NapCat 等协议端。云端为真源，本地维护可重建的元数据索引。

> [!NOTE]
> 本插件属第三方工具，请遵守《QQ 用户协议》，按"现状"使用，风险自负。

## 功能概览

| 标签页 | 功能 |
| --- | --- |
| 文件 | 浏览/搜索/上传/下载/改名/移动/删除/新建目录；批量操作；右键菜单 |
| 相册 | 图片/视频画廊浏览；媒体上传 |
| 精华 | 长文本保存（自动分段）/查看/删除 |
| 网盘 | OpenList 双向转存（群文件 ↔ 网盘）；目录浏览；任务管理 |
| 任务 | 任务台账（排队/运行/暂停/完成/失败）；暂停/继续/中断/撤销 |
| 群组 | 群列表管理；批量改名/备注/排序；框选多选 |
| 配置 | 插件配置中心（敏感项脱敏、重载提示） |

## 快速开始

1. 将插件目录放入 AstrBot `data/plugins/`（勿用符号链接）
2. 重启 AstrBot，进入 WebUI → 插件配置页
3. 配置 `managed_groups`（留空 = 全部群可管理）、`request_interval_ms` 等
4. 插件页面 → 群云存储管理

## 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/cssync [群号]` | 同步群云存储索引 |
| `/csfiles [群号] [页]` | 文件列表 |
| `/csfile <id> [群号]` | 文件详情 + 下载链接 |
| `/cssave [群号] <标题> <正文>` | 保存文本为群精华 |
| `/csfetch [群号] <URL> [文件名]` | 拉取外部文件入库 |
| `/csarchive <id> [群号]` | 转存文件到网盘 |
| `/csbridge status` | 网盘桥接状态 |
| `/cshelp` | 帮助 |

## 主要配置项

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `managed_groups` | `[]` | 受管群白名单；留空 = 全部可管理 |
| `request_interval_ms` | `1000` | API 请求间隔（ms），防风控 |
| `auto_scan_interval_hours` | `6` | 定时同步周期（小时）；0 = 关闭 |
| `download_server_enabled` | `false` | 本机下载服务开关 |
| `openlist_enabled` | `false` | OpenList 网盘桥接开关 |

完整配置见 `_conf_schema.json`。

## 许可

[AGPL v3](LICENSE)
