# astrbot-plugin-collect-skill

AstrBot 技能汇总插件。当前首个版本（v1）提供 **cron 任务管理技能**：

- 支持 `/cron ...` 命令式控制
- 支持自然关键词（如 `创建cron ...`）
- 支持创建、修改、删除、启用、禁用、列表查询
- 出错时返回可读中文报错（例如 cron 格式错误、任务 ID 不存在）

## 功能说明（v1）

### 1. 通过 `/` 命令管理

- `/cron 帮助`
- `/cron 添加 任务名 | */5 * * * * | 提醒内容`
- `/cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容`
- `/cron 删除 任务ID`
- `/cron 启用 任务ID`
- `/cron 禁用 任务ID`
- `/cron 列表`

说明：修改时不改的字段可填 `-`。

### 2. 通过关键词管理

- `创建cron 任务名 | */5 * * * * | 提醒内容`
- `修改cron 任务ID | 新任务名 | 新cron表达式 | 新提醒内容`
- `删除cron 任务ID`
- `查看cron`

### 3. 任务触发行为

任务到点后，插件会向创建任务时所在会话主动发送提醒消息：

`[cron提醒 #ID] 任务名 + 提醒内容 + cron表达式`

## 版本规划

- `v1.0.0`：cron 管理（创建/修改/删除/启停/列表）+ 主动提醒 + 可读错误提示
- 后续版本：按你的迭代继续扩展更多综合技能能力

## 参考

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
