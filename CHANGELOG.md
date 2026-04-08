# Changelog

All notable changes to this project will be documented in this file.

## v2.1.2 - 2026-04-06

### Added
- Added execution timeout control (`execution_timeout_seconds`) to prevent hung send calls.
- Added consecutive failure counter and optional auto-disable policy (`auto_disable_after_failures`).
- Added recent execution log buffer per task, with `/cron 日志 任务ID [条数]` support.
- Added `/cron 列表` filter & pagination options (`启用/禁用/异常/页/每页/关键词`).
- Added `future_execution_mode` to control scheduler ownership (`local_fallback` / `platform`).

### Changed
- Improved task list output with consecutive failure count.
- Updated metadata and README/README_EN for new command and config options.

### Compatibility
- Existing data remains compatible; new fields are auto-filled with safe defaults.

## v2.1.1 - 2026-04-04

### Changed
- Renamed plugin package to `astrbot_plugin_tschedule`.
- Updated display name to `Astrbot计划任务提醒`.
- Updated plugin register id to `tschedule` and plugin payload tag to `tschedule`.
- Renamed local fallback store file to `.tschedule_store_v2.json`.

### Compatibility
- Kept compatibility with old local store file `.collect_skill_store_v2.json` for seamless migration.

## v2.1.0 - 2026-04-04

### Added
- Added per-task timezone support (`tz=...`) with plugin-level default timezone.
- Added missed-trigger policy (`miss=catch_up|skip`) for catch-up or skip behavior.
- Added retry strategy options (`fixed|exponential`) and configurable retry interval.
- Added session-level task cap (`session_task_limit`) to prevent accidental overload.
- Added duplicate task detection (`duplicate_check`) within the same session.
- Added next trigger preview in task creation response.
- Added tests for cron validation, option parsing, and next-run calculation.

### Changed
- Extended `/cron 添加` and `/cron 单次` to accept optional key-value parameters.
- Extended `/cron 修改` and `/cron 修改单次` with the same optional parameter model.
- Enriched task list/log output with timezone, retry strategy, retry interval, and missed policy.
- Improved cron validation error messages with clearer field context (minute/hour/day/month/week).
- Updated plugin metadata and docs to `v2.1.0`.

### Compatibility
- Existing task data remains compatible and is read with safe defaults for new fields.
- Existing command formats remain usable; new parameters are optional.

## v2.0.4 - 2026-04-04

### Added
- Added admin-only cron controls with WebUI config support (`admin_only_cron`, `admin_ids`).
- Added per-task retry count and default retry config.
- Added synchronization to AstrBot future task list.
- Added LLM tools for assistant-driven task operations.
- Added keyword-based admin ID matching (supports partial QQ/Discord style identifiers).

### Changed
- Merged todo behavior into `/cron 单次`.
- Removed `/todo` and `/skill` command paths from active command set.
- Updated README with system prompt template and WebUI hot-update guidance.
