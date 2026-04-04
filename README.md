# astrbot-plugin-collect-skill

一个面向 AstrBot 的提醒调度插件，当前版本 `v2.0.4`，聚焦 `cron + 单次提醒`。

## 功能概览

- 周期任务：支持创建、修改、删除、启用、禁用、列表、立即执行、日志查询
- 单次提醒：已并入 `/cron` 命令（原 `todo` 能力）
- 权限控制：可在插件配置中开启“仅管理员可操作 cron”
- 重试机制：支持任务级自定义重试次数
- 主动能力同步：任务会同步到 AstrBot future task 列表
- LLM 工具支持：可由主助手识别自然语言后调用工具创建任务

## 安装与使用

1. 将本插件放入 AstrBot 插件目录并启用。  
2. 在会话中使用 `/cron 帮助` 查看指令。  
3. 如需自然语言创建提醒，建议给主助手配置本仓库的 [SYSTEM_PROMPT_TEMPLATE.md](/Users/shangtang/Documents/代码/astrbot_plugin_collect_skill/SYSTEM_PROMPT_TEMPLATE.md)。

## 命令说明（统一 /cron）

- `/cron 添加 任务名 | */5 * * * * | 提醒内容 | 可选重试次数`
- `/cron 单次 任务名 | 2026-04-05 09:30 | 提醒内容 | 可选重试次数`
- `/cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容 | 可选重试次数`
- `/cron 修改单次 任务ID | 新任务名 | 新执行时间 | 新提醒内容 | 可选重试次数`
- `/cron 删除 任务ID`
- `/cron 启用 任务ID`
- `/cron 禁用 任务ID`
- `/cron 列表`
- `/cron 日志 任务ID`
- `/cron 立即执行 任务ID`

说明：
- 单次时间支持 `YYYY-MM-DD HH:MM` 或 ISO datetime。  
- 本版本不再提供 `/todo`、`/skill` 命令。  

## WebUI 插件配置

本插件已提供 WebUI 配置菜单（由 `_conf_schema.json` 定义）：

- `admin_only_cron`：是否仅允许管理员操作 cron（默认开启）
- `admin_ids`：插件管理员列表，支持多个用户 ID
- `default_retry_times`：新建任务默认重试次数

说明：
- 当 `admin_only_cron=true` 时，非管理员只能查看任务，不能创建/修改/删除/启停/立即执行。  
- 任务可在命令中覆盖重试次数，不填时使用 `default_retry_times`。  

## LLM 工具接口

给主助手调用的工具如下：

- `create_cron_task(name, cron_expression, reminder)`
- `create_once_reminder(name, run_at, reminder)`
- `list_cron_tasks()`
- `delete_cron_task(task_id)`

推荐策略：主助手负责自然语言理解与时间解析，插件负责执行与调度。

## 参考

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
