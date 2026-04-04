# astrbot-plugin-collect-skill

AstrBot 技能插件（当前版本 `v2.0.3`），当前聚焦在 `cron`：

- 周期 cron 任务管理（创建/修改/删除/启停/列表）
- 单次提醒（原 todo 能力已并入 cron）
- 与 AstrBot future task 自动同步（创建/修改/删除/启停）
- 提供 LLM 工具，由主助手自行识别自然语言并调用

## 1. 命令用法（统一为 /cron）

- `/cron 帮助`
- `/cron 添加 任务名 | */5 * * * * | 提醒内容`
- `/cron 单次 任务名 | 2026-04-05 09:30 | 提醒内容`
- `/cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容`
- `/cron 修改单次 任务ID | 新任务名 | 新执行时间 | 新提醒内容`
- `/cron 删除 任务ID`
- `/cron 启用 任务ID`
- `/cron 禁用 任务ID`
- `/cron 列表`
- `/cron 日志 任务ID`
- `/cron 立即执行 任务ID`

说明：
- 原 `/todo`、`/skill` 已移除
- 单次时间支持：`YYYY-MM-DD HH:MM` 或 ISO datetime

## 2. 自然语言策略

插件不再直接做自然语言时间解析。  
自然语言交给主助手判断，再调用以下 LLM 工具：

- `create_cron_task`：创建周期任务
- `create_once_reminder`：创建单次提醒
- `list_cron_tasks`：查询当前会话任务
- `delete_cron_task`：删除任务

## 3. 版本策略

- 单个任务完成：只提升小版本（`x.y.z` 的 `z`）
- 完整阶段收束：再考虑提升大版本（`x.y`）

版本记录：
- `v1.0.0`：基础 cron 管理
- `v2.0.0`：cron 增强 + todo + skill + 审计
- `v2.0.1`：自然语言提醒解析
- `v2.0.2`：cron 与 future task 同步
- `v2.0.3`：移除 todo/skill/审计，统一 cron，新增 LLM 工具入口

## 参考

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
