import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


KV_KEY_CRON_TASKS_V2 = "cron_tasks_v2"
KV_KEY_CRON_NEXT_ID_V2 = "cron_next_id_v2"

# 旧版本键（v1.0.0），用于自动迁移
KV_KEY_TASKS_V1 = "cron_tasks_v1"
KV_KEY_NEXT_ID_V1 = "cron_next_id_v1"


@dataclass
class CronTask:
    task_id: int
    name: str
    reminder: str
    unified_msg_origin: str
    creator_id: str = ""

    # 周期任务字段
    cron_expr: str = ""

    # 单次任务字段
    run_once: bool = False
    run_at: str = ""  # ISO datetime string

    timezone_name: str = "Asia/Shanghai"
    enabled: bool = True
    retry_times: int = 0
    last_run_minute: str = ""
    last_error: str = ""
    last_run_at: str = ""
    future_job_id: str = ""


@register("collect_skill", "Tango", "AstrBot 技能汇总（v2.0.4: admin + retry）", "2.0.4")
class CollectSkillPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.plugin_config = config
        self.tasks: Dict[int, CronTask] = {}
        self.next_task_id: int = 1

        self._scheduler_task: Optional[asyncio.Task] = None
        self._local_store_path = Path(__file__).resolve().parent / ".collect_skill_store_v2.json"
        self._legacy_local_store_path = Path(__file__).resolve().parent / ".cron_tasks_v1.json"

    async def initialize(self):
        await self._load_state()
        await self._sync_all_tasks_to_future_list()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[collect_skill] scheduler started, cron=%s", len(self.tasks))

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    # ---------- LLM Tools ----------
    @filter.llm_tool(name="create_cron_task")
    async def llm_create_cron_task(
        self,
        event: AstrMessageEvent,
        name: str,
        cron_expression: str,
        reminder: str,
        retry_times: int = -1,
    ) -> str:
        """创建周期 cron 任务。

        Args:
            name(string): 任务名称。
            cron_expression(string): cron 表达式（5 段），例如 0 9 * * *。
            reminder(string): 提醒内容。
            retry_times(int): 重试次数，缺省时使用插件配置默认值。
        """
        self._ensure_admin(event)
        retry_part = "" if int(retry_times) < 0 else str(int(retry_times))
        payload = f"{name} | {cron_expression} | {reminder} | {retry_part}"
        return await self._create_task(payload, event, run_once=False)

    @filter.llm_tool(name="create_once_reminder")
    async def llm_create_once_reminder(
        self,
        event: AstrMessageEvent,
        name: str,
        run_at: str,
        reminder: str,
        retry_times: int = -1,
    ) -> str:
        """创建单次提醒任务（原 todo 能力合并到 cron）。

        Args:
            name(string): 任务名称。
            run_at(string): 执行时间，建议格式 YYYY-MM-DD HH:MM 或 ISO datetime。
            reminder(string): 提醒内容。
            retry_times(int): 重试次数，缺省时使用插件配置默认值。
        """
        self._ensure_admin(event)
        retry_part = "" if int(retry_times) < 0 else str(int(retry_times))
        payload = f"{name} | {run_at} | {reminder} | {retry_part}"
        return await self._create_task(payload, event, run_once=True)

    @filter.llm_tool(name="list_cron_tasks")
    async def llm_list_cron_tasks(self, event: AstrMessageEvent) -> str:
        """列出当前会话的 cron 任务。"""
        return self._list_tasks(event.unified_msg_origin)

    @filter.llm_tool(name="delete_cron_task")
    async def llm_delete_cron_task(self, event: AstrMessageEvent, task_id: str) -> str:
        """删除 cron 任务。

        Args:
            task_id(string): 任务 ID。
        """
        self._ensure_admin(event)
        tid = self._safe_int(task_id, "任务 ID")
        return await self._delete_task(tid)

    # ---------- 命令入口 ----------
    @filter.command("cron", alias={"定时", "cron任务", "提醒"})
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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_command_router(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text or text.startswith("/"):
            return

        # 只保留显式关键词，避免插件误判自然语言。
        if not self._looks_like_cron_keyword(text):
            return

        try:
            resp = await self._handle_cron_text(text, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] keyword command failed: %s", e)
            yield event.plain_result(self._friendly_error(e))

    # ---------- cron skill ----------
    async def _handle_cron_text(self, body: str, event: AstrMessageEvent) -> str:
        normalized = body.strip()

        if normalized.startswith(("添加 ", "创建 ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event, run_once=False)

        if normalized.startswith(("单次 ", "一次 ", "待办 ", "提醒一次 ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event, run_once=True)

        if normalized.startswith(("修改 ", "更新 ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._update_task(payload)

        if normalized.startswith(("修改单次 ", "更新单次 ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._update_once_task(payload)

        if normalized.startswith("删除 ") or normalized.startswith("移除 "):
            self._ensure_admin(event)
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._delete_task(task_id)

        if normalized.startswith("启用 "):
            self._ensure_admin(event)
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._toggle_task(task_id, True)

        if normalized.startswith("禁用 "):
            self._ensure_admin(event)
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._toggle_task(task_id, False)

        if normalized.startswith("日志 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return self._cron_task_log(task_id, event.unified_msg_origin)

        if normalized.startswith("立即执行 "):
            self._ensure_admin(event)
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._cron_run_now(task_id, event)

        if normalized in {"列表", "查看", "查看任务", "列出", "list", "ls"}:
            return self._list_tasks(event.unified_msg_origin)

        if normalized in {"help", "帮助", "?", "-h", "--help"}:
            return self._cron_help_text()

        # 关键词模式
        if normalized.startswith(("创建cron ", "新建cron ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event, run_once=False)

        if normalized.startswith(("创建待办 ", "新建待办 ", "创建提醒 ")):
            self._ensure_admin(event)
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event, run_once=True)

        if normalized in {"查看cron", "列出cron", "cron列表"}:
            return self._list_tasks(event.unified_msg_origin)

        return (
            "[E_CRON_UNKNOWN] 没有识别到可执行的 cron 指令。\n"
            "输入 `/cron 帮助` 查看格式。自然语言提醒请让主助手调用工具：create_cron_task / create_once_reminder。"
        )

    async def _create_task(self, payload: str, event: AstrMessageEvent, run_once: bool) -> str:
        parts = self._split_payload(payload)
        if len(parts) not in {3, 4}:
            if run_once:
                raise ValueError("[E_PARAM] 单次提醒格式应为：任务名 | 执行时间 | 提醒内容 | 可选重试次数")
            raise ValueError("[E_PARAM] 创建格式应为：任务名 | cron表达式 | 提醒内容 | 可选重试次数")

        name, expr_or_time, reminder = parts
        if not name:
            raise ValueError("[E_PARAM] 任务名不能为空")
        if not reminder:
            raise ValueError("[E_PARAM] 提醒内容不能为空")

        cron_expr = ""
        run_at_iso = ""
        retry_times = self._default_retry_times()
        if run_once:
            run_at_dt = self._parse_run_at(expr_or_time)
            run_at_iso = run_at_dt.isoformat()
        else:
            cron_expr = expr_or_time
            self._validate_cron_expr(cron_expr)
        if len(parts) == 4 and parts[3] != "":
            retry_times = self._safe_int(parts[3], "重试次数")
            if retry_times < 0:
                raise ValueError("[E_PARAM] 重试次数不能为负数")

        task = CronTask(
            task_id=self.next_task_id,
            name=name,
            reminder=reminder,
            unified_msg_origin=event.unified_msg_origin,
            creator_id=self._actor_id(event),
            cron_expr=cron_expr,
            run_once=run_once,
            run_at=run_at_iso,
            enabled=True,
            retry_times=retry_times,
        )

        self.tasks[task.task_id] = task
        self.next_task_id += 1

        await self._sync_task_to_future_list(task)
        await self._save_state()

        if run_once:
            return (
                f"已创建单次提醒任务 #{task.task_id}\n"
                f"名称：{task.name}\n"
                f"执行时间：{task.run_at}\n"
                f"提醒：{task.reminder}\n"
                f"重试次数：{task.retry_times}"
            )

        return (
            f"已创建 cron 任务 #{task.task_id}\n"
            f"名称：{task.name}\n"
            f"表达式：{task.cron_expr}\n"
            f"提醒：{task.reminder}\n"
            f"重试次数：{task.retry_times}"
        )

    async def _update_task(self, payload: str) -> str:
        parts = self._split_payload(payload)
        if len(parts) not in {4, 5}:
            raise ValueError("[E_PARAM] 修改格式应为：任务ID | 任务名 | cron表达式 | 提醒内容 | 可选重试次数（不改填 -）")

        task_id = self._safe_int(parts[0], "任务 ID")
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")
        if task.run_once:
            raise ValueError("[E_PARAM] 该任务是单次提醒，请使用 `/cron 修改单次 ...`")

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
        if len(parts) == 5 and parts[4] != "-":
            retry_times = self._safe_int(parts[4], "重试次数")
            if retry_times < 0:
                raise ValueError("[E_PARAM] 重试次数不能为负数")
            task.retry_times = retry_times

        task.last_run_minute = ""
        await self._sync_task_to_future_list(task)
        await self._save_state()
        return f"任务 #{task_id} 已更新。"

    async def _update_once_task(self, payload: str) -> str:
        parts = self._split_payload(payload)
        if len(parts) not in {4, 5}:
            raise ValueError("[E_PARAM] 修改单次格式应为：任务ID | 任务名 | 执行时间 | 提醒内容 | 可选重试次数（不改填 -）")

        task_id = self._safe_int(parts[0], "任务 ID")
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")
        if not task.run_once:
            raise ValueError("[E_PARAM] 该任务是周期任务，请使用 `/cron 修改 ...`")

        new_name, new_time, new_reminder = parts[1], parts[2], parts[3]

        if new_name != "-":
            if not new_name:
                raise ValueError("[E_PARAM] 任务名不能为空")
            task.name = new_name

        if new_time != "-":
            task.run_at = self._parse_run_at(new_time).isoformat()

        if new_reminder != "-":
            if not new_reminder:
                raise ValueError("[E_PARAM] 提醒内容不能为空")
            task.reminder = new_reminder
        if len(parts) == 5 and parts[4] != "-":
            retry_times = self._safe_int(parts[4], "重试次数")
            if retry_times < 0:
                raise ValueError("[E_PARAM] 重试次数不能为负数")
            task.retry_times = retry_times

        task.last_run_minute = ""
        await self._sync_task_to_future_list(task)
        await self._save_state()
        return f"单次任务 #{task_id} 已更新。"

    async def _delete_task(self, task_id: int) -> str:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        await self._delete_task_from_future_list(task)
        del self.tasks[task_id]
        await self._save_state()
        return f"任务 #{task_id} 已删除。"

    async def _toggle_task(self, task_id: int, enabled: bool) -> str:
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")
        task.enabled = enabled
        await self._sync_task_to_future_list(task)
        await self._save_state()
        return f"任务 #{task_id} 已{'启用' if enabled else '禁用'}。"

    def _list_tasks(self, current_umo: str) -> str:
        items = [t for t in self.tasks.values() if t.unified_msg_origin == current_umo]
        if not items:
            return "当前会话还没有任务。"

        lines = ["当前会话任务列表："]
        for t in sorted(items, key=lambda x: x.task_id):
            status = "启用" if t.enabled else "禁用"
            future_status = t.future_job_id if t.future_job_id else "未同步"
            kind = "单次" if t.run_once else "周期"
            schedule = t.run_at if t.run_once else t.cron_expr
            health = "正常" if not t.last_error else f"异常:{t.last_error}"
            lines.append(
                f"#{t.task_id} [{kind}/{status}] {t.name} | {schedule} | {t.reminder} | 重试:{t.retry_times} | {health} | future:{future_status}"
            )
        return "\n".join(lines)

    def _cron_task_log(self, task_id: int, current_umo: str) -> str:
        task = self.tasks.get(task_id)
        if not task or task.unified_msg_origin != current_umo:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        schedule = task.run_at if task.run_once else task.cron_expr
        return (
            f"任务 #{task.task_id} 日志\n"
            f"类型：{'单次' if task.run_once else '周期'}\n"
            f"名称：{task.name}\n"
            f"计划：{schedule}\n"
            f"重试次数：{task.retry_times}\n"
            f"上次执行：{task.last_run_at or '无'}\n"
            f"上次错误：{task.last_error or '无'}\n"
            f"future_job_id：{task.future_job_id or '无'}"
        )

    async def _cron_run_now(self, task_id: int, event: AstrMessageEvent) -> str:
        task = self.tasks.get(task_id)
        if not task or task.unified_msg_origin != event.unified_msg_origin:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        ok = await self._execute_task(task, datetime.now(), reason="manual")
        await self._save_state()
        if ok:
            if task.run_once:
                await self._delete_task(task.task_id)
                return f"单次任务 #{task_id} 已执行并移除。"
            return f"任务 #{task_id} 已立即执行。"
        return f"任务 #{task_id} 执行失败：{task.last_error or '未知错误'}"

    async def _scheduler_loop(self):
        while True:
            changed = False
            now = datetime.now()
            now_minute_key = now.strftime("%Y-%m-%d %H:%M")

            to_delete: list[int] = []
            for task in list(self.tasks.values()):
                if not task.enabled:
                    continue

                # 已同步到 AstrBot Future Task，由平台侧调度，避免双重提醒。
                if task.future_job_id and self._get_cron_manager() is not None:
                    continue

                if task.run_once:
                    if task.last_run_minute == now_minute_key:
                        continue
                    run_at_dt = self._parse_run_at(task.run_at)
                    if now >= run_at_dt:
                        ok = await self._execute_task(task, now, reason="once")
                        task.last_run_minute = now_minute_key
                        changed = True
                        if ok:
                            to_delete.append(task.task_id)
                    continue

                if task.last_run_minute == now_minute_key:
                    continue

                if self._cron_match(task.cron_expr, now):
                    ok = await self._execute_task(task, now, reason="cron")
                    task.last_run_minute = now_minute_key
                    if not ok and not task.last_error:
                        task.last_error = "执行失败"
                    changed = True

            for task_id in to_delete:
                if task_id in self.tasks:
                    del self.tasks[task_id]
                    changed = True

            if changed:
                await self._save_state()

            await asyncio.sleep(20)

    async def _execute_task(self, task: CronTask, now: datetime, reason: str) -> bool:
        text = f"[提醒 #{task.task_id}] {task.name}\n{task.reminder}\n"
        if task.run_once:
            text += f"(单次: {task.run_at})"
        else:
            text += f"(cron: {task.cron_expr})"

        max_attempts = 1 + max(0, int(task.retry_times))
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

    # ---------- future task sync ----------
    def _get_cron_manager(self):
        return getattr(self.context, "cron_manager", None)

    def _build_future_payload(self, task: CronTask) -> dict:
        return {
            "session": task.unified_msg_origin,
            "sender_id": task.creator_id or "unknown",
            "note": task.reminder,
            "retry_times": task.retry_times,
            "origin": "plugin",
            "plugin": "collect_skill",
            "plugin_task_id": task.task_id,
            "run_at": task.run_at if task.run_once else None,
        }

    async def _sync_task_to_future_list(self, task: CronTask):
        cron_mgr = self._get_cron_manager()
        if cron_mgr is None:
            return

        payload = self._build_future_payload(task)
        description = task.reminder
        cron_expression = None if task.run_once else task.cron_expr
        run_at_dt = self._parse_run_at(task.run_at) if task.run_once else None

        try:
            if task.future_job_id:
                job = await cron_mgr.update_job(
                    task.future_job_id,
                    name=task.name,
                    cron_expression=cron_expression,
                    description=description,
                    enabled=task.enabled,
                    timezone=task.timezone_name,
                    payload=payload,
                    run_once=task.run_once,
                )
                if job is not None:
                    return
                task.future_job_id = ""

            job = await cron_mgr.add_active_job(
                name=task.name,
                cron_expression=cron_expression,
                payload=payload,
                description=description,
                timezone=task.timezone_name,
                enabled=task.enabled,
                run_once=task.run_once,
                run_at=run_at_dt,
                persistent=True,
            )
            task.future_job_id = str(getattr(job, "job_id", "") or "")
        except Exception as e:
            logger.warning("[collect_skill] sync future task failed for #%s: %s", task.task_id, e)

    async def _delete_task_from_future_list(self, task: CronTask):
        cron_mgr = self._get_cron_manager()
        if cron_mgr is None or not task.future_job_id:
            return
        try:
            await cron_mgr.delete_job(task.future_job_id)
            task.future_job_id = ""
        except Exception as e:
            logger.warning("[collect_skill] delete future task failed for #%s: %s", task.task_id, e)

    async def _sync_all_tasks_to_future_list(self):
        cron_mgr = self._get_cron_manager()
        if cron_mgr is None:
            return
        changed = False
        for task in self.tasks.values():
            before = task.future_job_id
            await self._sync_task_to_future_list(task)
            if task.future_job_id != before:
                changed = True
        if changed:
            await self._save_state()

    # ---------- helper ----------
    async def _send_text(self, unified_msg_origin: str, text: str):
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(text)
            await self.context.send_message(unified_msg_origin, chain)
        except Exception:
            await self.context.send_message(unified_msg_origin, text)

    def _extract_cmd_body(self, raw: str) -> str:
        if raw.startswith("/"):
            parts = raw[1:].split(" ", 1)
            return parts[1].strip() if len(parts) > 1 else ""
        parts = raw.split(" ", 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _parse_run_at(self, text: str) -> datetime:
        value = text.strip()
        if not value:
            raise ValueError("[E_PARAM] 执行时间不能为空")
        try:
            return datetime.fromisoformat(value)
        except Exception:
            pass

        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M")
        except Exception as e:
            raise ValueError("[E_PARAM] 执行时间格式错误，请使用 `YYYY-MM-DD HH:MM` 或 ISO datetime") from e

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
                r"^(创建cron|新建cron|修改cron|更新cron|删除cron|查看cron|列出cron|cron列表|创建待办|新建待办|创建提醒)\b",
                text,
            )
        )

    def _split_payload(self, payload: str) -> list:
        normalized = payload.replace("｜", "|")
        return [p.strip() for p in normalized.split("|")]

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

    def _config_get(self, key: str, default: Any):
        if self.plugin_config is None:
            return default
        try:
            value = self.plugin_config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _admin_ids(self) -> set[str]:
        ids = set()
        cfg_ids = self._config_get("admin_ids", [])
        if isinstance(cfg_ids, list):
            for item in cfg_ids:
                s = str(item).strip()
                if s:
                    ids.add(s)

        # 兼容全局管理员配置
        try:
            global_cfg = self.context.get_config()
            for item in global_cfg.get("admins_id", []):
                s = str(item).strip()
                if s:
                    ids.add(s)
        except Exception:
            pass

        return ids

    def _admin_only_enabled(self) -> bool:
        return bool(self._config_get("admin_only_cron", True))

    def _default_retry_times(self) -> int:
        v = self._config_get("default_retry_times", 0)
        try:
            v = int(v)
        except Exception:
            v = 0
        return max(0, v)

    def _ensure_admin(self, event: AstrMessageEvent):
        if not self._admin_only_enabled():
            return
        actor = self._actor_id(event)
        if actor in self._admin_ids():
            return
        raise PermissionError("[E_FORBIDDEN] 当前插件配置为仅管理员可操作 cron 任务")

    def _cron_help_text(self) -> str:
        return (
            "cron 管理（v2.0.4）\n"
            "1) /cron 添加 任务名 | */5 * * * * | 提醒内容 | 可选重试次数\n"
            "2) /cron 单次 任务名 | 2026-04-05 09:30 | 提醒内容 | 可选重试次数\n"
            "   说明：原 todo 已并入单次提醒\n"
            "3) /cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容 | 可选重试次数\n"
            "4) /cron 修改单次 任务ID | 新任务名 | 新执行时间 | 新提醒内容 | 可选重试次数\n"
            "5) /cron 删除 任务ID\n"
            "6) /cron 启用 任务ID\n"
            "7) /cron 禁用 任务ID\n"
            "8) /cron 列表\n"
            "9) /cron 日志 任务ID\n"
            "10) /cron 立即执行 任务ID\n"
            "\n"
            "自然语言识别已交给主助手，请由助手调用工具：\n"
            "- create_cron_task（周期）\n"
            "- create_once_reminder（单次）\n"
            "管理员控制：可在插件 WebUI 配置中设置 admin_ids 与 admin_only_cron。"
        )

    # ---------- store ----------
    async def _load_state(self):
        cron_data = None

        migrated_from_v1 = False

        try:
            cron_data = await self.get_kv_data(KV_KEY_CRON_TASKS_V2, None)
            self.next_task_id = int(await self.get_kv_data(KV_KEY_CRON_NEXT_ID_V2, 1))
        except Exception:
            cron_data = None

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

        if cron_data is None and self._local_store_path.exists():
            try:
                obj = json.loads(self._local_store_path.read_text(encoding="utf-8"))
                cron_data = obj.get("cron_tasks", [])
                self.next_task_id = int(obj.get("cron_next_id", 1))
            except Exception as e:
                logger.exception("[collect_skill] load local state failed: %s", e)

        if cron_data is None and self._legacy_local_store_path.exists():
            try:
                obj = json.loads(self._legacy_local_store_path.read_text(encoding="utf-8"))
                cron_data = obj.get("tasks", [])
                self.next_task_id = int(obj.get("next_id", 1))
                migrated_from_v1 = True
            except Exception as e:
                logger.exception("[collect_skill] load local v1 state failed: %s", e)

        cron_data = cron_data or []

        self.tasks = self._deserialize_cron_tasks(cron_data)
        self.next_task_id = max(self.next_task_id, max(self.tasks.keys(), default=0) + 1)

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
                    reminder=str(item.get("reminder", "")).strip(),
                    unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                    creator_id=str(item.get("creator_id", "")),
                    cron_expr=str(item.get("cron_expr", "")).strip(),
                    run_once=bool(item.get("run_once", False)),
                    run_at=str(item.get("run_at", "")),
                    timezone_name=str(item.get("timezone_name", "Asia/Shanghai")),
                    enabled=bool(item.get("enabled", True)),
                    retry_times=max(0, int(item.get("retry_times", 0))),
                    last_run_minute=str(item.get("last_run_minute", "")),
                    last_error=str(item.get("last_error", "")),
                    last_run_at=str(item.get("last_run_at", "")),
                    future_job_id=str(item.get("future_job_id", "")),
                )
                if task.task_id > 0 and task.name and task.reminder and task.unified_msg_origin:
                    if task.run_once:
                        if task.run_at:
                            tasks[task.task_id] = task
                    else:
                        if task.cron_expr:
                            tasks[task.task_id] = task
            except Exception as e:
                logger.warning("[collect_skill] skip invalid cron task item: %s err=%s", item, e)
        return tasks

    async def _save_state(self):
        cron_payload = [asdict(t) for t in self.tasks.values()]

        kv_ok = False
        try:
            await self.put_kv_data(KV_KEY_CRON_TASKS_V2, cron_payload)
            await self.put_kv_data(KV_KEY_CRON_NEXT_ID_V2, self.next_task_id)
            kv_ok = True
        except Exception:
            kv_ok = False

        if not kv_ok:
            obj = {
                "cron_tasks": cron_payload,
                "cron_next_id": self.next_task_id,
            }
            self._local_store_path.write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
