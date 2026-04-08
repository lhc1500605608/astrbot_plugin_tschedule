# Astrbot Task Scheduler Reminder

[中文](./README.md) | English

`Astrbot计划任务提醒` is an AstrBot plugin focused on reliable and manageable scheduled reminders.

## Features

- Unified `/cron` command for recurring and one-time reminders
- Create, update, delete, enable/disable, run-now, list, and log operations
- Retry controls: retry count, retry strategy, retry interval
- Execution timeout and auto-disable threshold for repeated failures
- Timezone support and missed-trigger policy (`catch_up` / `skip`)
- Admin control, duplicate task detection, session task limit
- Future-task synchronization with AstrBot

## Quick Start

1. Install and enable plugin: `astrbot_plugin_tschedule`
2. Run `/cron 帮助` in a chat session
3. Create your first scheduled task with the examples below

## Command Examples

- `/cron 添加 Morning Brief | 0 9 * * * | Check today's plan`
- `/cron 单次 Meeting Reminder | 2026-04-05 09:30 | Meeting starts in 10 minutes`
- `/cron 列表`
- `/cron 列表 enabled error page=1 size=10 keyword=daily`
- `/cron 日志 1 20`
- `/cron 立即执行 1`

## Optional Parameters

You can append optional parameters when creating/updating tasks:

- `retry=2`: retry times on failure
- `tz=Asia/Shanghai`: task timezone
- `miss=catch_up|skip`: missed-trigger policy
- `retry_strategy=fixed|exponential`: retry strategy
- `retry_interval=5`: retry interval in seconds

## List & Log Enhancements

- `/cron 列表` supports filters and paging: `enabled|disabled`, `error`, `page=1`, `size=10`, `keyword=xxx`
- `/cron 日志 task_id [count]` shows recent execution entries (default 10, max 50)

## WebUI Config (Selected)

- `admin_only_cron`: only admins can operate cron tasks
- `admin_ids`: extra plugin-level admins (global AstrBot admins are also used)
- `default_timezone`: default timezone
- `default_retry_times`: default retry count
- `execution_timeout_seconds`: timeout for single send attempt
- `auto_disable_after_failures`: auto-disable threshold for consecutive failures (`0` to disable)
- `future_execution_mode`: execution mode (`local_fallback` recommended, `platform` for full platform ownership)
- `session_task_limit`: task limit per session
- `duplicate_check`: duplicate task detection switch

See full config in [_conf_schema.json](./_conf_schema.json).

## Assistant Integration

For natural-language assistant integration, see [SYSTEM_PROMPT_TEMPLATE.md](./SYSTEM_PROMPT_TEMPLATE.md).

## License

This project is licensed under **GNU Affero General Public License v3.0 (AGPL-3.0)**.  
See [LICENSE](./LICENSE) for details.

## Links

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Dev Docs (CN)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Dev Docs (EN)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
