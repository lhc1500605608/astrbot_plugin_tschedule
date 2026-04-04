# astrbot-plugin-collect-skill

AstrBot 复合技能插件（当前版本 `v2.0.2`），包含：

- `cron` 任务管理（创建/修改/删除/启停/列表）
- `cron` 增强能力（预提醒、失败重试、立即执行、任务日志）
- `cron` 与 AstrBot future task 自动同步（创建/修改/删除/启停）
- `cron` 自然语言提醒解析（每天/每周/每小时/每隔N分钟）
- 模板变量（`{task_id}`、`{task_name}`、`{now}`、`{todo_summary}`）
- `todo` 待办管理（添加/完成/删除/列表）
- `todo` 与 `cron` 复合（定时待办汇总）
- `skill` 导入导出与审计日志
- 自动兼容迁移：`cron_tasks_v1` -> `cron_tasks_v2`

## 1. cron 命令

- `/cron 帮助`
- `/cron 添加 任务名 | */5 * * * * | 提醒内容`
- `/cron 添加 任务名 | */5 * * * * | 提醒内容 | 预提醒=5,重试=2`
- `/cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容 | 可选参数`
- `/cron 删除 任务ID`
- `/cron 启用 任务ID`
- `/cron 禁用 任务ID`
- `/cron 列表`
- `/cron 日志 任务ID`
- `/cron 立即执行 任务ID`

说明：
- 修改时不改的字段可填 `-`
- 可选参数也支持简写：`5,2`（预提醒分钟,重试次数）

关键词也支持：
- `创建cron ...`
- `修改cron ...`
- `删除cron ...`
- `查看cron`

自然语言也支持（无需手写 cron 表达式）：
- `每天 09:30 提醒我 喝水`
- `每周一 18:00 提醒我 提交周报`
- `每小时提醒我 站起来活动`
- `每隔 20 分钟提醒我 看一下消息`

## 2. todo 命令

- `/todo 帮助`
- `/todo 添加 今天要完成文档`
- `/todo 完成 1`
- `/todo 删除 1`
- `/todo 列表`
- `/todo 定时汇总 每天待办汇总 | 0 9 * * *`

关键词也支持：
- `创建待办 ...`
- `完成待办 1`
- `删除待办 1`
- `查看待办`

## 3. skill 命令

- `/skill 帮助`
- `/skill 导出`
- `/skill 导入 {json}`
- `/skill 审计`

说明：
- 若配置了环境变量 `COLLECT_SKILL_ADMIN_USERS`（逗号分隔用户 ID），`/skill 导入` 仅管理员可执行

## 4. 权限与审计

- 删除/修改等高风险操作默认按“任务创建者/管理员”限制
- 每次关键操作写入审计日志（可用 `/skill 审计` 查看最近 20 条）

## 5. 版本说明

- 版本策略：单个任务完成仅提升小版本（`x.y.z` 的 `z`），完整阶段完成再提升大版本（`x.y`）。
- `v1.0.0`：基础 cron 管理
- `v2.0.0`：cron 增强 + todo + skill 管理 + 审计 + 迁移兼容
- `v2.0.1`：新增自然语言提醒解析与命令引导
- `v2.0.2`：新增 cron 与 AstrBot future task 列表同步

## 参考

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
