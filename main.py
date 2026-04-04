import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


KV_KEY_CRON_TASKS_V2 = "cron_tasks_v2"
KV_KEY_CRON_NEXT_ID_V2 = "cron_next_id_v2"
KV_KEY_TODO_ITEMS_V1 = "todo_items_v1"
KV_KEY_TODO_NEXT_ID_V1 = "todo_next_id_v1"
KV_KEY_AUDIT_LOGS_V1 = "audit_logs_v1"
KV_KEY_AUDIT_NEXT_ID_V1 = "audit_next_id_v1"

# 旧版本键（v1.0.0），用于自动迁移
KV_KEY_TASKS_V1 = "cron_tasks_v1"
KV_KEY_NEXT_ID_V1 = "cron_next_id_v1"


@dataclass
class CronTask:
    task_id: int
    name: str
    cron_expr: str
    reminder: str
    unified_msg_origin: str
    timezone_name: str = "Asia/Shanghai"
    creator_id: str = ""
    enabled: bool = True
    last_run_minute: str = ""
    last_pre_notify_key: str = ""
    pre_notify_minutes: int = 0
    retry_times: int = 0
    last_error: str = ""
    last_run_at: str = ""


@dataclass
class TodoItem:
    todo_id: int
    content: str
    unified_msg_origin: str
    creator_id: str
    done: bool = False
    created_at: str = ""
    done_at: str = ""


@dataclass
class AuditLog:
    log_id: int
    ts: str
    actor_id: str
    action: str
    target: str
    result: str
    unified_msg_origin: str


@register("collect_skill", "Tango", "AstrBot 技能汇总（v2.0.1: cron+todo+import/export）", "2.0.1")
class CollectSkillPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tasks: Dict[int, CronTask] = {}
        self.next_task_id: int = 1

        self.todos: Dict[int, TodoItem] = {}
        self.next_todo_id: int = 1

        self.audit_logs: List[AuditLog] = []
        self.next_audit_id: int = 1

        self._scheduler_task: Optional[asyncio.Task] = None
        self._local_store_path = Path(__file__).resolve().parent / ".collect_skill_store_v2.json"
        self._legacy_local_store_path = Path(__file__).resolve().parent / ".cron_tasks_v1.json"

        admin_raw = os.getenv("COLLECT_SKILL_ADMIN_USERS", "")
        self.admin_user_ids = {x.strip() for x in admin_raw.split(",") if x.strip()}

    async def initialize(self):
        await self._load_state()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[collect_skill] scheduler started, cron=%s todo=%s", len(self.tasks), len(self.todos))

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    # ---------- 命令入口 ----------
    @filter.command("cron", alias={"定时", "cron任务"})
    async def cron_command(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        body = self._extract_cmd_body(raw)
        if not body or body in {"help", "帮助", "?", "-h", "--help"}:
            yield event.plain_result(self._cron_help_text())
            return

        try:
            resp = await self._handle_cron_text(body, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] /cron command failed: %s", e)
            yield event.plain_result(self._friendly_error(e))

    @filter.command("todo", alias={"待办"})
    async def todo_command(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        body = self._extract_cmd_body(raw)
        if not body or body in {"help", "帮助", "?", "-h", "--help"}:
            yield event.plain_result(self._todo_help_text())
            return

        try:
            resp = await self._handle_todo_text(body, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] /todo command failed: %s", e)
            yield event.plain_result(self._friendly_error(e))

    @filter.command("skill", alias={"技能"})
    async def skill_command(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        body = self._extract_cmd_body(raw)
        if not body or body in {"help", "帮助", "?", "-h", "--help"}:
            yield event.plain_result(self._skill_help_text())
            return

        try:
            resp = await self._handle_skill_text(body, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] /skill command failed: %s", e)
            yield event.plain_result(self._friendly_error(e))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_command_router(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text or text.startswith("/"):
            return

        try:
            if self._looks_like_cron_keyword(text):
                resp = await self._handle_cron_text(text, event)
            elif self._looks_like_todo_keyword(text):
                resp = await self._handle_todo_text(text, event)
            elif self._looks_like_natural_cron_intent(text):
                parsed = self._parse_natural_cron_intent(text)
                if parsed:
                    payload = f"{parsed['name']} | {parsed['cron_expr']} | {parsed['reminder']}"
                    resp = await self._create_task(payload, event)
                    await self._append_audit(event, "cron_create_natural", "cron_task", "ok")
                    resp += "\n(已通过自然语言解析创建，你也可以用 `/cron 帮助` 查看精确命令格式)"
                else:
                    resp = self._cron_natural_usage_hint(text)
            else:
                return
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] keyword command failed: %s", e)
            yield event.plain_result(self._friendly_error(e))

    # ---------- cron skill ----------
    async def _handle_cron_text(self, body: str, event: AstrMessageEvent) -> str:
        normalized = body.strip()

        if normalized.startswith(("添加 ", "创建 ")):
            payload = normalized.split(" ", 1)[1].strip()
            msg = await self._create_task(payload, event)
            await self._append_audit(event, "cron_create", "cron_task", "ok")
            return msg

        if normalized.startswith(("修改 ", "更新 ")):
            payload = normalized.split(" ", 1)[1].strip()
            msg = await self._update_task(payload, event)
            await self._append_audit(event, "cron_update", "cron_task", "ok")
            return msg

        if normalized.startswith("删除 ") or normalized.startswith("移除 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            msg = await self._delete_task(task_id, event)
            await self._append_audit(event, "cron_delete", f"cron_task#{task_id}", "ok")
            return msg

        if normalized.startswith("启用 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            msg = await self._toggle_task(task_id, True)
            await self._append_audit(event, "cron_enable", f"cron_task#{task_id}", "ok")
            return msg

        if normalized.startswith("禁用 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            msg = await self._toggle_task(task_id, False)
            await self._append_audit(event, "cron_disable", f"cron_task#{task_id}", "ok")
            return msg

        if normalized.startswith("日志 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return self._cron_task_log(task_id, event.unified_msg_origin)

        if normalized.startswith("立即执行 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            msg = await self._cron_run_now(task_id, event)
            await self._append_audit(event, "cron_run_now", f"cron_task#{task_id}", "ok")
            return msg

        if normalized in {"列表", "查看", "查看任务", "列出", "list", "ls"}:
            return self._list_tasks(event.unified_msg_origin)

        if normalized in {"help", "帮助", "?", "-h", "--help"}:
            return self._cron_help_text()

        # 关键词模式：创建cron ... / 修改cron ...
        if normalized.startswith(("创建cron ", "新建cron ")):
            payload = normalized.split(" ", 1)[1].strip()
            msg = await self._create_task(payload, event)
            await self._append_audit(event, "cron_create_keyword", "cron_task", "ok")
            return msg

        if normalized.startswith(("修改cron ", "更新cron ")):
            payload = normalized.split(" ", 1)[1].strip()
            msg = await self._update_task(payload, event)
            await self._append_audit(event, "cron_update_keyword", "cron_task", "ok")
            return msg

        if normalized.startswith("删除cron "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            msg = await self._delete_task(task_id, event)
            await self._append_audit(event, "cron_delete_keyword", f"cron_task#{task_id}", "ok")
            return msg

        if normalized in {"查看cron", "列出cron", "cron列表"}:
            return self._list_tasks(event.unified_msg_origin)

        return (
            "[E_CRON_UNKNOWN] 没有识别到可执行的 cron 指令。\n"
            "输入 `/cron 帮助` 查看格式，或使用关键词：`创建cron` / `修改cron` / `删除cron` / `查看cron`。"
        )

    async def _create_task(self, payload: str, event: AstrMessageEvent) -> str:
        parts = self._split_payload(payload)
        if len(parts) not in {3, 4}:
            raise ValueError("[E_PARAM] 创建格式应为：任务名 | cron表达式 | 提醒内容 | 可选参数")

        name, cron_expr, reminder = parts[0], parts[1], parts[2]
        if not name:
            raise ValueError("[E_PARAM] 任务名不能为空")
        if not reminder:
            raise ValueError("[E_PARAM] 提醒内容不能为空")

        self._validate_cron_expr(cron_expr)

        pre_notify_minutes = 0
        retry_times = 0
        if len(parts) == 4 and parts[3] not in {"", "-"}:
            opts = self._parse_options(parts[3])
            pre_notify_minutes = opts.get("pre_notify_minutes", 0)
            retry_times = opts.get("retry_times", 0)

        task = CronTask(
            task_id=self.next_task_id,
            name=name,
            cron_expr=cron_expr,
            reminder=reminder,
            unified_msg_origin=event.unified_msg_origin,
            timezone_name="Asia/Shanghai",
            creator_id=self._actor_id(event),
            enabled=True,
            pre_notify_minutes=pre_notify_minutes,
            retry_times=retry_times,
        )

        self.tasks[task.task_id] = task
        self.next_task_id += 1
        await self._save_state()

        return (
            f"已创建 cron 任务 #{task.task_id}\n"
            f"名称：{task.name}\n"
            f"表达式：{task.cron_expr}\n"
            f"提醒：{task.reminder}\n"
            f"预提醒：{task.pre_notify_minutes} 分钟\n"
            f"重试：{task.retry_times} 次"
        )

    async def _update_task(self, payload: str, event: AstrMessageEvent) -> str:
        parts = self._split_payload(payload)
        if len(parts) not in {4, 5}:
            raise ValueError("[E_PARAM] 修改格式应为：任务ID | 任务名 | cron表达式 | 提醒内容 | 可选参数（不改填 -）")

        task_id = self._safe_int(parts[0], "任务 ID")
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        # 允许原作者或管理员更新
        self._ensure_owner_or_admin(task.creator_id, event, "[E_FORBIDDEN] 你无权修改该任务")

        new_name, new_cron, new_reminder = parts[1], parts[2], parts[3]

        if new_name != "-":
            if not new_name:
                raise ValueError("[E_PARAM] 任务名不能为空")
            task.name = new_name

        if new_cron != "-":
            self._validate_cron_expr(new_cron)
            task.cron_expr = new_cron

        if new_reminder != "-":
            if not new_reminder:
                raise ValueError("[E_PARAM] 提醒内容不能为空")
            task.reminder = new_reminder

        if len(parts) == 5 and parts[4] not in {"", "-"}:
            opts = self._parse_options(parts[4])
            task.pre_notify_minutes = opts.get("pre_notify_minutes", task.pre_notify_minutes)
            task.retry_times = opts.get("retry_times", task.retry_times)

        task.last_run_minute = ""
        task.last_pre_notify_key = ""
        await self._save_state()
        return f"任务 #{task_id} 已更新。"

    async def _delete_task(self, task_id: int, event: AstrMessageEvent) -> str:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        self._ensure_owner_or_admin(task.creator_id, event, "[E_FORBIDDEN] 你无权删除该任务")

        del self.tasks[task_id]
        await self._save_state()
        return f"任务 #{task_id} 已删除。"

    async def _toggle_task(self, task_id: int, enabled: bool) -> str:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")
        task.enabled = enabled
        await self._save_state()
        return f"任务 #{task_id} 已{'启用' if enabled else '禁用'}。"

    def _list_tasks(self, current_umo: str) -> str:
        items = [t for t in self.tasks.values() if t.unified_msg_origin == current_umo]
        if not items:
            return "当前会话还没有 cron 任务。"

        lines = ["当前会话的 cron 任务："]
        for t in sorted(items, key=lambda x: x.task_id):
            status = "启用" if t.enabled else "禁用"
            health = "正常" if not t.last_error else f"异常:{t.last_error}"
            lines.append(
                f"#{t.task_id} [{status}] {t.name} | {t.cron_expr} | 预提醒:{t.pre_notify_minutes}m 重试:{t.retry_times} | {health}"
            )
        return "\n".join(lines)

    def _cron_task_log(self, task_id: int, current_umo: str) -> str:
        task = self.tasks.get(task_id)
        if not task or task.unified_msg_origin != current_umo:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        return (
            f"cron 任务 #{task.task_id} 日志\n"
            f"名称：{task.name}\n"
            f"上次执行：{task.last_run_at or '无'}\n"
            f"上次错误：{task.last_error or '无'}\n"
            f"预提醒：{task.pre_notify_minutes} 分钟\n"
            f"重试次数：{task.retry_times}"
        )

    async def _cron_run_now(self, task_id: int, event: AstrMessageEvent) -> str:
        task = self.tasks.get(task_id)
        if not task or task.unified_msg_origin != event.unified_msg_origin:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        ok = await self._execute_task(task, datetime.now(), reason="manual")
        await self._save_state()
        if ok:
            return f"任务 #{task_id} 已立即执行。"
        return f"任务 #{task_id} 执行失败：{task.last_error or '未知错误'}"

    async def _scheduler_loop(self):
        while True:
            changed = False
            now = datetime.now()
            now_minute_key = now.strftime("%Y-%m-%d %H:%M")

            for task in list(self.tasks.values()):
                if not task.enabled:
                    continue

                # 预提醒：在触发前 N 分钟推送一次。
                if task.pre_notify_minutes > 0:
                    target_time = now + timedelta(minutes=task.pre_notify_minutes)
                    target_key = target_time.strftime("%Y-%m-%d %H:%M")
                    if task.last_pre_notify_key != target_key and self._cron_match(task.cron_expr, target_time):
                        await self._send_pre_notification(task, target_time)
                        task.last_pre_notify_key = target_key
                        changed = True

                # 正式触发。
                if task.last_run_minute == now_minute_key:
                    continue

                if self._cron_match(task.cron_expr, now):
                    ok = await self._execute_task(task, now, reason="cron")
                    task.last_run_minute = now_minute_key
                    if not ok and not task.last_error:
                        task.last_error = "执行失败"
                    changed = True

            if changed:
                await self._save_state()

            await asyncio.sleep(20)

    async def _send_pre_notification(self, task: CronTask, target_time: datetime):
        text = (
            f"[cron预提醒 #{task.task_id}] {task.name}\n"
            f"将在 {target_time.strftime('%Y-%m-%d %H:%M')} 触发\n"
            f"({task.cron_expr})"
        )
        await self._send_text(task.unified_msg_origin, text)

    async def _execute_task(self, task: CronTask, now: datetime, reason: str) -> bool:
        rendered = self._render_reminder(task.reminder, task, now)
        text = f"[cron提醒 #{task.task_id}] {task.name}\n{rendered}\n({task.cron_expr})"

        max_attempts = max(1, 1 + int(task.retry_times))
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                await self._send_text(task.unified_msg_origin, text)
                task.last_error = ""
                task.last_run_at = now.strftime("%Y-%m-%d %H:%M:%S")
                return True
            except Exception as e:
                last_err = str(e)
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 * attempt, 5))

        task.last_error = f"{reason}失败: {last_err[:120]}"
        return False

    # ---------- todo skill ----------
    async def _handle_todo_text(self, body: str, event: AstrMessageEvent) -> str:
        normalized = body.strip()

        if normalized.startswith(("添加 ", "创建 ")):
            content = normalized.split(" ", 1)[1].strip()
            msg = await self._todo_add(content, event)
            await self._append_audit(event, "todo_add", "todo_item", "ok")
            return msg

        if normalized.startswith("完成 "):
            todo_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "待办 ID")
            msg = await self._todo_done(todo_id, event)
            await self._append_audit(event, "todo_done", f"todo#{todo_id}", "ok")
            return msg

        if normalized.startswith(("删除 ", "移除 ")):
            todo_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "待办 ID")
            msg = await self._todo_delete(todo_id, event)
            await self._append_audit(event, "todo_delete", f"todo#{todo_id}", "ok")
            return msg

        if normalized in {"列表", "查看", "查看待办", "列出", "list", "ls"}:
            return self._todo_list(event.unified_msg_origin)

        if normalized.startswith("定时汇总 "):
            payload = normalized.split(" ", 1)[1].strip()
            msg = await self._todo_schedule_summary(payload, event)
            await self._append_audit(event, "todo_schedule_summary", "cron_task", "ok")
            return msg

        # 关键词模式
        if normalized.startswith("创建待办 "):
            content = normalized.split(" ", 1)[1].strip()
            msg = await self._todo_add(content, event)
            await self._append_audit(event, "todo_add_keyword", "todo_item", "ok")
            return msg

        if normalized.startswith("完成待办 "):
            todo_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "待办 ID")
            msg = await self._todo_done(todo_id, event)
            await self._append_audit(event, "todo_done_keyword", f"todo#{todo_id}", "ok")
            return msg

        if normalized.startswith("删除待办 "):
            todo_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "待办 ID")
            msg = await self._todo_delete(todo_id, event)
            await self._append_audit(event, "todo_delete_keyword", f"todo#{todo_id}", "ok")
            return msg

        if normalized in {"查看待办", "待办列表", "列出待办"}:
            return self._todo_list(event.unified_msg_origin)

        if normalized in {"help", "帮助", "?", "-h", "--help"}:
            return self._todo_help_text()

        return "[E_TODO_UNKNOWN] 未识别到待办指令，输入 `/todo 帮助` 查看说明。"

    async def _todo_add(self, content: str, event: AstrMessageEvent) -> str:
        if not content:
            raise ValueError("[E_PARAM] 待办内容不能为空")

        item = TodoItem(
            todo_id=self.next_todo_id,
            content=content,
            unified_msg_origin=event.unified_msg_origin,
            creator_id=self._actor_id(event),
            done=False,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        self.todos[item.todo_id] = item
        self.next_todo_id += 1
        await self._save_state()
        return f"已添加待办 #{item.todo_id}：{item.content}"

    async def _todo_done(self, todo_id: int, event: AstrMessageEvent) -> str:
        item = self.todos.get(todo_id)
        if not item or item.unified_msg_origin != event.unified_msg_origin:
            raise ValueError(f"[E_NOT_FOUND] 待办 #{todo_id} 不存在")

        self._ensure_owner_or_admin(item.creator_id, event, "[E_FORBIDDEN] 你无权完成该待办")
        item.done = True
        item.done_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self._save_state()
        return f"待办 #{todo_id} 已完成。"

    async def _todo_delete(self, todo_id: int, event: AstrMessageEvent) -> str:
        item = self.todos.get(todo_id)
        if not item or item.unified_msg_origin != event.unified_msg_origin:
            raise ValueError(f"[E_NOT_FOUND] 待办 #{todo_id} 不存在")

        self._ensure_owner_or_admin(item.creator_id, event, "[E_FORBIDDEN] 你无权删除该待办")
        del self.todos[todo_id]
        await self._save_state()
        return f"待办 #{todo_id} 已删除。"

    def _todo_list(self, current_umo: str) -> str:
        items = [t for t in self.todos.values() if t.unified_msg_origin == current_umo]
        if not items:
            return "当前会话还没有待办。"

        lines = ["当前会话待办列表："]
        for item in sorted(items, key=lambda x: x.todo_id):
            status = "已完成" if item.done else "待处理"
            lines.append(f"#{item.todo_id} [{status}] {item.content}")
        return "\n".join(lines)

    async def _todo_schedule_summary(self, payload: str, event: AstrMessageEvent) -> str:
        parts = self._split_payload(payload)
        if len(parts) != 2:
            raise ValueError("[E_PARAM] 定时汇总格式应为：任务名 | cron表达式")

        name, cron_expr = parts
        if not name:
            raise ValueError("[E_PARAM] 任务名不能为空")
        self._validate_cron_expr(cron_expr)

        task = CronTask(
            task_id=self.next_task_id,
            name=name,
            cron_expr=cron_expr,
            reminder="待办定时汇总\n{todo_summary}",
            unified_msg_origin=event.unified_msg_origin,
            timezone_name="Asia/Shanghai",
            creator_id=self._actor_id(event),
            enabled=True,
        )
        self.tasks[task.task_id] = task
        self.next_task_id += 1
        await self._save_state()

        return f"已创建待办汇总 cron 任务 #{task.task_id}（{task.cron_expr}）。"

    # ---------- skill management ----------
    async def _handle_skill_text(self, body: str, event: AstrMessageEvent) -> str:
        normalized = body.strip()

        if normalized == "导出":
            data = self._export_current_session(event.unified_msg_origin)
            await self._append_audit(event, "skill_export", "session_data", "ok")
            return "导出成功：\n```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```"

        if normalized.startswith("导入 "):
            self._ensure_admin_for_global_write(event)
            raw_json = normalized.split(" ", 1)[1].strip()
            msg = await self._import_current_session(raw_json, event)
            await self._append_audit(event, "skill_import", "session_data", "ok")
            return msg

        if normalized in {"审计", "日志"}:
            return self._audit_log_list(event.unified_msg_origin)

        if normalized in {"help", "帮助", "?", "-h", "--help"}:
            return self._skill_help_text()

        return "[E_SKILL_UNKNOWN] 未识别到技能管理命令，输入 `/skill 帮助` 查看说明。"

    def _export_current_session(self, current_umo: str) -> dict:
        cron_items = [
            asdict(t) for t in self.tasks.values() if t.unified_msg_origin == current_umo
        ]
        todo_items = [
            asdict(t) for t in self.todos.values() if t.unified_msg_origin == current_umo
        ]
        return {
            "version": "2.0.0",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cron": cron_items,
            "todo": todo_items,
        }

    async def _import_current_session(self, raw_json: str, event: AstrMessageEvent) -> str:
        try:
            data = json.loads(raw_json)
        except Exception as e:
            raise ValueError("[E_PARAM] 导入 JSON 不合法") from e

        current_umo = event.unified_msg_origin
        actor = self._actor_id(event)

        cron_count = 0
        for item in data.get("cron", []):
            try:
                cron_expr = str(item.get("cron_expr", "")).strip()
                self._validate_cron_expr(cron_expr)
                task = CronTask(
                    task_id=self.next_task_id,
                    name=str(item.get("name", "导入任务")).strip() or "导入任务",
                    cron_expr=cron_expr,
                    reminder=str(item.get("reminder", "")).strip() or "(空提醒)",
                    unified_msg_origin=current_umo,
                    timezone_name="Asia/Shanghai",
                    creator_id=actor,
                    enabled=bool(item.get("enabled", True)),
                    pre_notify_minutes=max(0, int(item.get("pre_notify_minutes", 0))),
                    retry_times=max(0, int(item.get("retry_times", 0))),
                )
                self.tasks[task.task_id] = task
                self.next_task_id += 1
                cron_count += 1
            except Exception:
                continue

        todo_count = 0
        for item in data.get("todo", []):
            try:
                todo = TodoItem(
                    todo_id=self.next_todo_id,
                    content=str(item.get("content", "")).strip() or "导入待办",
                    unified_msg_origin=current_umo,
                    creator_id=actor,
                    done=bool(item.get("done", False)),
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.todos[todo.todo_id] = todo
                self.next_todo_id += 1
                todo_count += 1
            except Exception:
                continue

        await self._save_state()
        return f"导入完成：cron {cron_count} 条，todo {todo_count} 条。"

    def _audit_log_list(self, current_umo: str) -> str:
        items = [x for x in self.audit_logs if x.unified_msg_origin == current_umo]
        if not items:
            return "当前会话暂无审计日志。"

        lines = ["最近审计日志（最多20条）："]
        for x in items[-20:]:
            lines.append(f"#{x.log_id} {x.ts} actor={x.actor_id} {x.action} {x.target} => {x.result}")
        return "\n".join(lines)

    async def _append_audit(self, event: AstrMessageEvent, action: str, target: str, result: str):
        log = AuditLog(
            log_id=self.next_audit_id,
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            actor_id=self._actor_id(event),
            action=action,
            target=target,
            result=result,
            unified_msg_origin=event.unified_msg_origin,
        )
        self.next_audit_id += 1
        self.audit_logs.append(log)
        if len(self.audit_logs) > 500:
            self.audit_logs = self.audit_logs[-500:]
        await self._save_state()

    # ---------- helper ----------
    async def _send_text(self, unified_msg_origin: str, text: str):
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(text)
            await self.context.send_message(unified_msg_origin, chain)
        except Exception:
            await self.context.send_message(unified_msg_origin, text)

    def _render_reminder(self, template: str, task: CronTask, now: datetime) -> str:
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        text = template
        text = text.replace("{task_id}", str(task.task_id))
        text = text.replace("{task_name}", task.name)
        text = text.replace("{now}", now_text)

        if "{todo_summary}" in text:
            text = text.replace("{todo_summary}", self._todo_summary_text(task.unified_msg_origin))

        return text

    def _todo_summary_text(self, current_umo: str) -> str:
        items = [t for t in self.todos.values() if t.unified_msg_origin == current_umo]
        if not items:
            return "当前没有待办。"

        undone = [x for x in items if not x.done]
        done = [x for x in items if x.done]

        lines = [f"待办总数：{len(items)}，待处理：{len(undone)}，已完成：{len(done)}"]
        for x in undone[:10]:
            lines.append(f"- [ ] #{x.todo_id} {x.content}")
        if len(undone) > 10:
            lines.append(f"... 其余 {len(undone) - 10} 项未展示")
        return "\n".join(lines)

    def _extract_cmd_body(self, raw: str) -> str:
        if raw.startswith("/"):
            parts = raw[1:].split(" ", 1)
            return parts[1].strip() if len(parts) > 1 else ""
        parts = raw.split(" ", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _parse_options(self, text: str) -> dict:
        # 支持：预提醒=5,重试=2 或 pre=5,retry=2 或 5,2
        result = {}
        s = text.strip().replace("，", ",").replace("；", ",")

        plain = [p.strip() for p in s.split(",") if p.strip()]
        if len(plain) == 2 and all(re.fullmatch(r"\d+", p) for p in plain):
            result["pre_notify_minutes"] = max(0, int(plain[0]))
            result["retry_times"] = max(0, int(plain[1]))
            return result

        for part in plain:
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            key = k.strip().lower()
            value = self._safe_int(v.strip(), f"参数 `{k}`")
            if value < 0:
                raise ValueError(f"[E_PARAM] 参数 `{k}` 不能为负数")

            if key in {"预提醒", "pre", "pre_notify", "pre_notify_minutes"}:
                result["pre_notify_minutes"] = value
            elif key in {"重试", "retry", "retry_times"}:
                result["retry_times"] = value

        return result

    def _ensure_owner_or_admin(self, owner_id: str, event: AstrMessageEvent, err_msg: str):
        if not owner_id:
            return

        actor = self._actor_id(event)
        if actor == owner_id or actor in self.admin_user_ids:
            return
        raise PermissionError(err_msg)

    def _ensure_admin_for_global_write(self, event: AstrMessageEvent):
        if not self.admin_user_ids:
            return
        actor = self._actor_id(event)
        if actor not in self.admin_user_ids:
            raise PermissionError("[E_FORBIDDEN] 当前命令仅管理员可执行")

    def _actor_id(self, event: AstrMessageEvent) -> str:
        for key in ("sender_id", "user_id", "userId", "uid"):
            value = getattr(event, key, None)
            if value is not None:
                return str(value)

        sender = getattr(event, "sender", None)
        if sender is not None:
            for key in ("id", "user_id", "uid"):
                value = getattr(sender, key, None)
                if value is not None:
                    return str(value)

        return "unknown"

    def _validate_cron_expr(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("[E_CRON] cron 表达式必须是 5 段：分 时 日 月 周")

        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        for i, f in enumerate(fields):
            self._parse_cron_field(f, ranges[i][0], ranges[i][1])

    def _cron_match(self, expr: str, now: datetime) -> bool:
        minute, hour, day, month, week = expr.split()
        values = [
            now.minute,
            now.hour,
            now.day,
            now.month,
            (now.weekday() + 1) % 7,
        ]
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        fields = [minute, hour, day, month, week]

        for field, value, (min_v, max_v) in zip(fields, values, ranges):
            allowed = self._parse_cron_field(field, min_v, max_v)
            if value not in allowed:
                return False
        return True

    def _parse_cron_field(self, field: str, min_v: int, max_v: int) -> set:
        result = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"[E_CRON] cron 字段 `{field}` 非法")

            if part == "*":
                result.update(range(min_v, max_v + 1))
                continue

            if "/" in part:
                base, step_str = part.split("/", 1)
                step = self._safe_int(step_str, f"cron 步长 `{part}`")
                if step <= 0:
                    raise ValueError(f"[E_CRON] cron 步长必须大于 0：`{part}`")

                if base == "*":
                    start, end = min_v, max_v
                elif "-" in base:
                    start_str, end_str = base.split("-", 1)
                    start = self._safe_int(start_str, f"cron 范围 `{part}`")
                    end = self._safe_int(end_str, f"cron 范围 `{part}`")
                else:
                    start = self._safe_int(base, f"cron 字段 `{part}`")
                    end = max_v

                self._check_range(start, min_v, max_v, f"cron 字段 `{part}`")
                self._check_range(end, min_v, max_v, f"cron 字段 `{part}`")
                if start > end:
                    raise ValueError(f"[E_CRON] cron 范围起始不能大于结束：`{part}`")

                result.update(range(start, end + 1, step))
                continue

            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start = self._safe_int(start_str, f"cron 范围 `{part}`")
                end = self._safe_int(end_str, f"cron 范围 `{part}`")
                self._check_range(start, min_v, max_v, f"cron 范围 `{part}`")
                self._check_range(end, min_v, max_v, f"cron 范围 `{part}`")
                if start > end:
                    raise ValueError(f"[E_CRON] cron 范围起始不能大于结束：`{part}`")
                result.update(range(start, end + 1))
                continue

            value = self._safe_int(part, f"cron 字段 `{part}`")
            self._check_range(value, min_v, max_v, f"cron 字段 `{part}`")
            result.add(value)

        if not result:
            raise ValueError(f"[E_CRON] cron 字段 `{field}` 解析后为空")
        return result

    def _check_range(self, value: int, min_v: int, max_v: int, label: str):
        if value < min_v or value > max_v:
            raise ValueError(f"[E_CRON] {label} 超出范围（{min_v}-{max_v}）")

    def _safe_int(self, text: str, label: str) -> int:
        try:
            return int(text)
        except Exception as e:
            raise ValueError(f"[E_PARAM] {label} 不是合法整数：`{text}`") from e

    def _friendly_error(self, e: Exception) -> str:
        if isinstance(e, (ValueError, PermissionError)):
            return str(e)
        return f"[E_INTERNAL] 未知错误：{str(e)}"

    def _looks_like_cron_keyword(self, text: str) -> bool:
        return bool(
            re.match(
                r"^(创建cron|新建cron|修改cron|更新cron|删除cron|查看cron|列出cron|cron列表)\b",
                text,
            )
        )

    def _looks_like_natural_cron_intent(self, text: str) -> bool:
        if re.search(r"(不要|别|取消).*提醒", text):
            return False
        if not re.search(r"(提醒我|提醒|通知我|叫我)", text):
            return False
        if re.search(r"(每隔\s*\d+\s*分钟|每小时|每天|每周[一二三四五六日天1-7])", text):
            return True
        return False

    def _parse_natural_cron_intent(self, text: str) -> Optional[dict]:
        normalized = " ".join(text.strip().split())

        # 每隔 N 分钟提醒我 ...
        m = re.match(
            r"^(?:请)?(?:帮我)?每隔\s*(\d{1,2})\s*分钟(?:提醒我|提醒|通知我|叫我)\s*(.+)$",
            normalized,
        )
        if m:
            step = int(m.group(1))
            if step < 1 or step > 59:
                return None
            reminder = self._clean_natural_reminder(m.group(2))
            return {
                "name": f"每隔{step}分钟提醒",
                "cron_expr": f"*/{step} * * * *",
                "reminder": reminder,
            }

        # 每小时提醒我 ...
        m = re.match(r"^(?:请)?(?:帮我)?每小时(?:提醒我|提醒|通知我|叫我)\s*(.+)$", normalized)
        if m:
            reminder = self._clean_natural_reminder(m.group(1))
            return {"name": "每小时提醒", "cron_expr": "0 * * * *", "reminder": reminder}

        # 每天 [早上/下午...] H[:M]/H点[M分] 提醒我 ...
        m = re.match(
            r"^(?:请)?(?:帮我)?每天(?:\s*(凌晨|早上|上午|中午|下午|晚上))?\s*(\d{1,2})(?:(?:[:：点时])\s*(\d{1,2}))?\s*(?:分)?(?:提醒我|提醒|通知我|叫我)\s*(.+)$",
            normalized,
        )
        if m:
            period = m.group(1) or ""
            hour = int(m.group(2))
            minute = int(m.group(3) or "0")
            hour = self._normalize_hour_with_period(hour, period)
            if hour is None or minute < 0 or minute > 59:
                return None
            reminder = self._clean_natural_reminder(m.group(4))
            return {
                "name": f"每天{hour:02d}:{minute:02d}提醒",
                "cron_expr": f"{minute} {hour} * * *",
                "reminder": reminder,
            }

        # 每周X [早上/下午...] H[:M]/H点[M分] 提醒我 ...
        m = re.match(
            r"^(?:请)?(?:帮我)?每周([一二三四五六日天1-7])(?:\s*(凌晨|早上|上午|中午|下午|晚上))?\s*(\d{1,2})(?:(?:[:：点时])\s*(\d{1,2}))?\s*(?:分)?(?:提醒我|提醒|通知我|叫我)\s*(.+)$",
            normalized,
        )
        if m:
            weekday = self._weekday_to_cron(m.group(1))
            period = m.group(2) or ""
            hour = int(m.group(3))
            minute = int(m.group(4) or "0")
            hour = self._normalize_hour_with_period(hour, period)
            if weekday is None or hour is None or minute < 0 or minute > 59:
                return None
            reminder = self._clean_natural_reminder(m.group(5))
            return {
                "name": f"每周{m.group(1)}{hour:02d}:{minute:02d}提醒",
                "cron_expr": f"{minute} {hour} * * {weekday}",
                "reminder": reminder,
            }

        return None

    def _normalize_hour_with_period(self, hour: int, period: str) -> Optional[int]:
        if hour < 0 or hour > 23:
            return None
        p = period.strip()
        if not p:
            return hour if hour <= 23 else None
        if p in {"凌晨", "早上", "上午"}:
            if hour == 12:
                return 0
            return hour if 0 <= hour <= 11 else None
        if p == "中午":
            if hour in {11, 12}:
                return hour
            if 1 <= hour <= 10:
                return hour + 12
            return None
        if p in {"下午", "晚上"}:
            if 1 <= hour <= 11:
                return hour + 12
            if hour in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}:
                return hour
            return None
        return None

    def _weekday_to_cron(self, text: str) -> Optional[int]:
        mapping = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "日": 0,
            "天": 0,
            "1": 1,
            "2": 2,
            "3": 3,
            "4": 4,
            "5": 5,
            "6": 6,
            "7": 0,
        }
        return mapping.get(text)

    def _clean_natural_reminder(self, text: str) -> str:
        value = text.strip()
        value = re.sub(r"^[，。,:：\\s]+", "", value)
        value = value.replace("|", "｜")
        if not value:
            value = "提醒事项"
        return value

    def _cron_natural_usage_hint(self, text: str) -> str:
        reminder = "提醒内容"
        m = re.search(r"(?:提醒我|提醒|通知我|叫我)\s*(.+)$", text)
        if m:
            reminder = self._clean_natural_reminder(m.group(1))
        return (
            "我识别到你在创建提醒任务，但时间表达还不够明确。\n"
            "你可以直接用这些自然语言格式：\n"
            "- 每天 09:30 提醒我 " + reminder + "\n"
            "- 每周一 18:00 提醒我 " + reminder + "\n"
            "- 每隔 15 分钟提醒我 " + reminder + "\n"
            "也可以用命令：\n"
            f"/cron 添加 提醒任务 | 30 9 * * * | {reminder}"
        )

    def _looks_like_todo_keyword(self, text: str) -> bool:
        return bool(
            re.match(
                r"^(创建待办|完成待办|删除待办|查看待办|列出待办|待办列表)\b",
                text,
            )
        )

    def _split_payload(self, payload: str) -> list:
        normalized = payload.replace("｜", "|")
        return [p.strip() for p in normalized.split("|")]

    # ---------- help text ----------
    def _cron_help_text(self) -> str:
        return (
            "cron 管理（v2）\n"
            "1) /cron 添加 任务名 | */5 * * * * | 提醒内容 | 预提醒=5,重试=2\n"
            "   最后一段可省略；也支持简写 `5,2`（预提醒分钟,重试次数）\n"
            "2) /cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容 | 可选参数\n"
            "   不修改的字段填 -\n"
            "3) /cron 删除 任务ID\n"
            "4) /cron 启用 任务ID\n"
            "5) /cron 禁用 任务ID\n"
            "6) /cron 列表\n"
            "7) /cron 日志 任务ID\n"
            "8) /cron 立即执行 任务ID\n"
            "\n"
            "自然语言也支持（常见格式）：每天/每周/每小时/每隔N分钟 + 提醒我...\n"
            "模板变量：{task_id} {task_name} {now} {todo_summary}\n"
            "关键词也支持：创建cron / 修改cron / 删除cron / 查看cron"
        )

    def _todo_help_text(self) -> str:
        return (
            "todo 管理（v1）\n"
            "1) /todo 添加 待办内容\n"
            "2) /todo 完成 待办ID\n"
            "3) /todo 删除 待办ID\n"
            "4) /todo 列表\n"
            "5) /todo 定时汇总 任务名 | cron表达式\n"
            "\n"
            "关键词也支持：创建待办 / 完成待办 / 删除待办 / 查看待办"
        )

    def _skill_help_text(self) -> str:
        return (
            "skill 管理（v2）\n"
            "1) /skill 导出\n"
            "2) /skill 导入 {json}\n"
            "3) /skill 审计\n"
            "\n"
            "说明：导入默认需要管理员（配置 COLLECT_SKILL_ADMIN_USERS）"
        )

    # ---------- store ----------
    async def _load_state(self):
        cron_data = None
        todo_data = None
        audit_data = None

        migrated_from_v1 = False

        # 先读 v2 KV。
        try:
            cron_data = await self.get_kv_data(KV_KEY_CRON_TASKS_V2, None)
            self.next_task_id = int(await self.get_kv_data(KV_KEY_CRON_NEXT_ID_V2, 1))

            todo_data = await self.get_kv_data(KV_KEY_TODO_ITEMS_V1, [])
            self.next_todo_id = int(await self.get_kv_data(KV_KEY_TODO_NEXT_ID_V1, 1))

            audit_data = await self.get_kv_data(KV_KEY_AUDIT_LOGS_V1, [])
            self.next_audit_id = int(await self.get_kv_data(KV_KEY_AUDIT_NEXT_ID_V1, 1))
        except Exception:
            cron_data = None

        # v2 KV 不存在时，尝试 v1 KV 迁移。
        if cron_data is None:
            try:
                old_cron = await self.get_kv_data(KV_KEY_TASKS_V1, None)
                old_next = await self.get_kv_data(KV_KEY_NEXT_ID_V1, 1)
                if old_cron is not None:
                    cron_data = old_cron
                    self.next_task_id = int(old_next)
                    migrated_from_v1 = True
            except Exception:
                pass

        # KV 都失败则读本地 v2。
        if cron_data is None and self._local_store_path.exists():
            try:
                obj = json.loads(self._local_store_path.read_text(encoding="utf-8"))
                cron_data = obj.get("cron_tasks", [])
                self.next_task_id = int(obj.get("cron_next_id", 1))

                todo_data = obj.get("todo_items", [])
                self.next_todo_id = int(obj.get("todo_next_id", 1))

                audit_data = obj.get("audit_logs", [])
                self.next_audit_id = int(obj.get("audit_next_id", 1))
            except Exception as e:
                logger.exception("[collect_skill] load local v2 state failed: %s", e)

        # 再兜底读本地 v1。
        if cron_data is None and self._legacy_local_store_path.exists():
            try:
                obj = json.loads(self._legacy_local_store_path.read_text(encoding="utf-8"))
                cron_data = obj.get("tasks", [])
                self.next_task_id = int(obj.get("next_id", 1))
                migrated_from_v1 = True
            except Exception as e:
                logger.exception("[collect_skill] load local v1 state failed: %s", e)

        cron_data = cron_data or []
        todo_data = todo_data or []
        audit_data = audit_data or []

        self.tasks = self._deserialize_cron_tasks(cron_data)
        self.next_task_id = max(self.next_task_id, max(self.tasks.keys(), default=0) + 1)

        self.todos = self._deserialize_todo_items(todo_data)
        self.next_todo_id = max(self.next_todo_id, max(self.todos.keys(), default=0) + 1)

        self.audit_logs = self._deserialize_audit_logs(audit_data)
        self.next_audit_id = max(self.next_audit_id, max((x.log_id for x in self.audit_logs), default=0) + 1)

        if migrated_from_v1:
            logger.info("[collect_skill] detected v1 data, writing migrated v2 store")
            await self._save_state()

    def _deserialize_cron_tasks(self, data: list) -> Dict[int, CronTask]:
        tasks: Dict[int, CronTask] = {}
        for item in data:
            try:
                task = CronTask(
                    task_id=int(item.get("task_id")),
                    name=str(item.get("name", "")).strip(),
                    cron_expr=str(item.get("cron_expr", "")).strip(),
                    reminder=str(item.get("reminder", "")).strip(),
                    unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                    timezone_name=str(item.get("timezone_name", "Asia/Shanghai")),
                    creator_id=str(item.get("creator_id", "")),
                    enabled=bool(item.get("enabled", True)),
                    last_run_minute=str(item.get("last_run_minute", "")),
                    last_pre_notify_key=str(item.get("last_pre_notify_key", "")),
                    pre_notify_minutes=max(0, int(item.get("pre_notify_minutes", 0))),
                    retry_times=max(0, int(item.get("retry_times", 0))),
                    last_error=str(item.get("last_error", "")),
                    last_run_at=str(item.get("last_run_at", "")),
                )
                if task.task_id > 0 and task.name and task.cron_expr and task.unified_msg_origin:
                    tasks[task.task_id] = task
            except Exception as e:
                logger.warning("[collect_skill] skip invalid cron task item: %s err=%s", item, e)
        return tasks

    def _deserialize_todo_items(self, data: list) -> Dict[int, TodoItem]:
        items: Dict[int, TodoItem] = {}
        for item in data:
            try:
                todo = TodoItem(
                    todo_id=int(item.get("todo_id")),
                    content=str(item.get("content", "")).strip(),
                    unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                    creator_id=str(item.get("creator_id", "")),
                    done=bool(item.get("done", False)),
                    created_at=str(item.get("created_at", "")),
                    done_at=str(item.get("done_at", "")),
                )
                if todo.todo_id > 0 and todo.content and todo.unified_msg_origin:
                    items[todo.todo_id] = todo
            except Exception as e:
                logger.warning("[collect_skill] skip invalid todo item: %s err=%s", item, e)
        return items

    def _deserialize_audit_logs(self, data: list) -> List[AuditLog]:
        logs: List[AuditLog] = []
        for item in data:
            try:
                log = AuditLog(
                    log_id=int(item.get("log_id")),
                    ts=str(item.get("ts", "")),
                    actor_id=str(item.get("actor_id", "")),
                    action=str(item.get("action", "")),
                    target=str(item.get("target", "")),
                    result=str(item.get("result", "")),
                    unified_msg_origin=str(item.get("unified_msg_origin", "")),
                )
                if log.log_id > 0:
                    logs.append(log)
            except Exception as e:
                logger.warning("[collect_skill] skip invalid audit log: %s err=%s", item, e)
        logs.sort(key=lambda x: x.log_id)
        return logs

    async def _save_state(self):
        cron_payload = [asdict(t) for t in self.tasks.values()]
        todo_payload = [asdict(t) for t in self.todos.values()]
        audit_payload = [asdict(t) for t in self.audit_logs]

        kv_ok = False
        try:
            await self.put_kv_data(KV_KEY_CRON_TASKS_V2, cron_payload)
            await self.put_kv_data(KV_KEY_CRON_NEXT_ID_V2, self.next_task_id)

            await self.put_kv_data(KV_KEY_TODO_ITEMS_V1, todo_payload)
            await self.put_kv_data(KV_KEY_TODO_NEXT_ID_V1, self.next_todo_id)

            await self.put_kv_data(KV_KEY_AUDIT_LOGS_V1, audit_payload)
            await self.put_kv_data(KV_KEY_AUDIT_NEXT_ID_V1, self.next_audit_id)
            kv_ok = True
        except Exception:
            kv_ok = False

        if not kv_ok:
            obj = {
                "cron_tasks": cron_payload,
                "cron_next_id": self.next_task_id,
                "todo_items": todo_payload,
                "todo_next_id": self.next_todo_id,
                "audit_logs": audit_payload,
                "audit_next_id": self.next_audit_id,
            }
            self._local_store_path.write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
