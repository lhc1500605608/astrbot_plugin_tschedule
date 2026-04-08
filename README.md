# Astrbot计划任务提醒

中文 | [English](./README_EN.md)

`Astrbot计划任务提醒` 是一个面向 AstrBot 的计划任务插件，专注于稳定、可治理的提醒调度能力。

## 特性

- 统一 `/cron` 命令，支持周期任务与单次提醒
- 支持任务创建、修改、删除、启用/禁用、立即执行、日志查询
- 支持重试次数、重试策略、重试间隔
- 支持执行超时与连续失败自动禁用（可配置）
- 支持时区、错过触发策略（补执行/跳过）
- 支持管理员控制、任务去重、单会话任务上限
- 可同步到 AstrBot future task 列表

## 快速开始

1. 安装并启用插件：`astrbot_plugin_tschedule`
2. 在会话中输入：`/cron 帮助`
3. 按帮助示例创建你的第一个任务

## 命令示例

- `/cron 添加 早报 | 0 9 * * * | 记得查看今日安排`
- `/cron 单次 开会提醒 | 2026-04-05 09:30 | 10分钟后会议开始`
- `/cron 列表`
- `/cron 列表 启用 异常 页=1 每页=10 关键词=晨会`
- `/cron 日志 1 20`
- `/cron 立即执行 1`

## 可选参数

创建/修改任务时可追加参数：

- `retry=2`：失败重试次数
- `tz=Asia/Shanghai`：任务时区
- `miss=catch_up|skip`：错过触发点策略
- `retry_strategy=fixed|exponential`：重试策略
- `retry_interval=5`：重试间隔（秒）

## 列表与日志增强

- `/cron 列表` 支持筛选和分页：`启用|禁用`、`异常`、`页=1`、`每页=10`、`关键词=xxx`
- `/cron 日志 任务ID [条数]` 支持查看最近 N 条执行记录（默认 10，最大 50）

## WebUI 配置（节选）

- `admin_only_cron`：是否仅管理员可操作
- `admin_ids`：插件附加管理员（默认也会读取 AstrBot 全局管理员）
- `default_timezone`：默认时区
- `default_retry_times`：默认重试次数
- `execution_timeout_seconds`：单次执行超时秒数
- `auto_disable_after_failures`：连续失败自动禁用阈值（0 表示关闭）
- `future_execution_mode`：future 执行模式（`local_fallback` 推荐，`platform` 为平台全托管）
- `session_task_limit`：单会话任务上限
- `duplicate_check`：是否启用重复任务检测

完整配置见 [_conf_schema.json](./_conf_schema.json)。

## 与助手联动

如需让主助手通过自然语言调用任务创建能力，可参考 [SYSTEM_PROMPT_TEMPLATE.md](./SYSTEM_PROMPT_TEMPLATE.md)。

## 许可证

本项目基于 **GNU Affero General Public License v3.0 (AGPL-3.0)** 开源。  
详情请参阅 [LICENSE](./LICENSE) 文件。

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档（中文）](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
