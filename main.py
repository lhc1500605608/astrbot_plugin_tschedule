import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

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
    retry_strategy: str = "fixed"  # fixed | exponential
    retry_interval_seconds: int = 2
    missed_policy: str = "catch_up"  # catch_up | skip
    last_run_minute: str = ""
    last_check_at: str = ""
    last_error: str = ""
    last_run_at: str = ""
    future_job_id: str = ""


@register("tschedule", "Tango", "Astrbot计划任务提醒（v2.1.1）", "2.1.1")
class TschedulePlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.plugin_config = config
        self.tasks: Dict[int, CronTask] = {}
        self.next_task_id: int = 1

        self._scheduler_task: Optional[asyncio.Task] = None
        (
            self._local_store_path,
            self._legacy_local_store_path_v2,
            self._legacy_local_store_path,
        ) = self._build_store_paths()

    def _build_store_paths(self) -> tuple[Path, Path, Path]:
        """
        持久化优先写入 data 目录，避免插件更新/重装时数据被覆盖。
        同时保留历史路径读取能力，保证平滑迁移。
        """
        plugin_root = Path(__file__).resolve().parent
        data_base_candidates: list[Path] = []

        for attr in ("data_path", "data_dir"):
            value = getattr(self.context, attr, None)
            if value:
                data_base_candidates.append(Path(str(value)))

        get_data_dir = getattr(self.context, "get_data_dir", None)
        if callable(get_data_dir):
            try:
                value = get_data_dir()
                if value:
                    data_base_candidates.append(Path(str(value)))
            except Exception:
                pass

        # 最后兜底到进程工作目录下 data。
        data_base_candidates.append(Path.cwd() / "data")

        data_dir = plugin_root / "data"
        for candidate in data_base_candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                data_dir = candidate / "astrbot_plugin_tschedule"
                data_dir.mkdir(parents=True, exist_ok=True)
                break
            except Exception:
                continue

        # 优先新路径，旧路径只用于读取迁移。
        local_store_path = data_dir / "tschedule_store_v2.json"
        legacy_local_store_path_v2 = plugin_root / ".collect_skill_store_v2.json"
        legacy_local_store_path = plugin_root / ".cron_tasks_v1.json"
        return local_store_path, legacy_local_store_path_v2, legacy_local_store_path

    async def initialize(self):
        await self._load_state()
        await self._sync_all_tasks_to_future_list()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[tschedule] scheduler started, cron=%s", len(self.tasks))

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
        timezone: str = "",
        missed_policy: str = "",
        retry_strategy: str = "",
        retry_interval_seconds: int = -1,
    ) -> str:
        """创建周期 cron 任务。

        Args:
            name(string): 任务名称。
            cron_expression(string): cron 表达式（5 段），例如 0 9 * * *。
            reminder(string): 提醒内容。
            retry_times(int): 重试次数，缺省时使用插件配置默认值。
            timezone(string): 时区，如 Asia/Shanghai。
            missed_policy(string): catch_up 或 skip。
            retry_strategy(string): fixed 或 exponential。
            retry_interval_seconds(int): 重试间隔秒数。
        """
        self._ensure_admin(event)
        extras = []
        if int(retry_times) >= 0:
            extras.append(str(int(retry_times)))
        if timezone:
            extras.append(f"tz={timezone}")
        if missed_policy:
            extras.append(f"miss={missed_policy}")
        if retry_strategy:
            extras.append(f"retry_strategy={retry_strategy}")
        if int(retry_interval_seconds) > 0:
            extras.append(f"retry_interval={int(retry_interval_seconds)}")
        payload = " | ".join([name, cron_expression, reminder] + extras)
        return await self._create_task(payload, event, run_once=False)

    @filter.llm_tool(name="create_once_reminder")
    async def llm_create_once_reminder(
        self,
        event: AstrMessageEvent,
        name: str,
        run_at: str,
        reminder: str,
        retry_times: int = -1,
        timezone: str = "",
        missed_policy: str = "",
        retry_strategy: str = "",
        retry_interval_seconds: int = -1,
    ) -> str:
        """创建单次提醒任务（原 todo 能力合并到 cron）。

        Args:
            name(string): 任务名称。
            run_at(string): 执行时间，建议格式 YYYY-MM-DD HH:MM 或 ISO datetime。
            reminder(string): 提醒内容。
            retry_times(int): 重试次数，缺省时使用插件配置默认值。
            timezone(string): 时区，如 Asia/Shanghai。
            missed_policy(string): catch_up 或 skip。
            retry_strategy(string): fixed 或 exponential。
            retry_interval_seconds(int): 重试间隔秒数。
        """
        self._ensure_admin(event)
        extras = []
        if int(retry_times) >= 0:
            extras.append(str(int(retry_times)))
        if timezone:
            extras.append(f"tz={timezone}")
        if missed_policy:
            extras.append(f"miss={missed_policy}")
        if retry_strategy:
            extras.append(f"retry_strategy={retry_strategy}")
        if int(retry_interval_seconds) > 0:
            extras.append(f"retry_interval={int(retry_interval_seconds)}")
        payload = " | ".join([name, run_at, reminder] + extras)
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
            logger.exception("[tschedule] /cron command failed: %s", e)
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
            logger.exception("[tschedule] keyword command failed: %s", e)
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

    async def _create_task(
        self, payload: str, event: AstrMessageEvent, run_once: bool
    ) -> str:
        parts = self._split_payload(payload)
        if len(parts) < 3:
            if run_once:
                raise ValueError(
                    "[E_PARAM] 单次提醒格式应为：任务名 | 执行时间 | 提醒内容 | 可选重试次数 | 可选参数"
                )
            raise ValueError(
                "[E_PARAM] 创建格式应为：任务名 | cron表达式 | 提醒内容 | 可选重试次数 | 可选参数"
            )

        name, expr_or_time, reminder = parts[0], parts[1], parts[2]
        if not name:
            raise ValueError("[E_PARAM] 任务名不能为空")
        if not reminder:
            raise ValueError("[E_PARAM] 提醒内容不能为空")

        self._check_session_task_limit(event.unified_msg_origin)

        cron_expr = ""
        run_at_iso = ""
        options = self._parse_task_options(parts[3:])
        retry_times = options["retry_times"]
        timezone_name = options["timezone_name"]
        missed_policy = options["missed_policy"]
        retry_strategy = options["retry_strategy"]
        retry_interval_seconds = options["retry_interval_seconds"]
        if run_once:
            run_at_dt = self._parse_run_at(expr_or_time, timezone_name)
            run_at_iso = run_at_dt.isoformat()
        else:
            cron_expr = expr_or_time
            self._validate_cron_expr(cron_expr)

        self._check_duplicate_task(
            unified_msg_origin=event.unified_msg_origin,
            run_once=run_once,
            name=name,
            reminder=reminder,
            cron_expr=cron_expr,
            run_at=run_at_iso,
            timezone_name=timezone_name,
            exclude_task_id=None,
        )

        task = CronTask(
            task_id=self.next_task_id,
            name=name,
            reminder=reminder,
            unified_msg_origin=event.unified_msg_origin,
            creator_id=self._actor_id(event),
            cron_expr=cron_expr,
            run_once=run_once,
            run_at=run_at_iso,
            timezone_name=timezone_name,
            enabled=True,
            retry_times=retry_times,
            retry_strategy=retry_strategy,
            retry_interval_seconds=retry_interval_seconds,
            missed_policy=missed_policy,
        )

        self.tasks[task.task_id] = task
        self.next_task_id += 1

        await self._sync_task_to_future_list(task)
        await self._save_state()

        preview = (
            task.run_at
            if task.run_once
            else self._format_dt(
                self._next_cron_time(task.cron_expr, task.timezone_name)
            )
        )
        preview_text = preview or "无法计算（请检查 cron 表达式）"

        if run_once:
            return (
                f"已创建单次提醒任务 #{task.task_id}\n"
                f"名称：{task.name}\n"
                f"执行时间：{task.run_at}\n"
                f"提醒：{task.reminder}\n"
                f"时区：{task.timezone_name}\n"
                f"错过策略：{task.missed_policy}\n"
                f"重试：{task.retry_times} 次，间隔 {task.retry_interval_seconds}s，策略 {task.retry_strategy}\n"
                f"下次触发：{preview_text}"
            )

        return (
            f"已创建 cron 任务 #{task.task_id}\n"
            f"名称：{task.name}\n"
            f"表达式：{task.cron_expr}\n"
            f"提醒：{task.reminder}\n"
            f"时区：{task.timezone_name}\n"
            f"错过策略：{task.missed_policy}\n"
            f"重试：{task.retry_times} 次，间隔 {task.retry_interval_seconds}s，策略 {task.retry_strategy}\n"
            f"下次触发：{preview_text}"
        )

    async def _update_task(self, payload: str) -> str:
        parts = self._split_payload(payload)
        if len(parts) < 4:
            raise ValueError(
                "[E_PARAM] 修改格式应为：任务ID | 任务名 | cron表达式 | 提醒内容 | 可选重试次数（不改填 -） | 可选参数"
            )

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
        options = self._parse_task_options(parts[4:], for_update=True)
        if options["retry_times"] is not None:
            task.retry_times = options["retry_times"]
        if options["timezone_name"]:
            task.timezone_name = options["timezone_name"]
        if options["missed_policy"]:
            task.missed_policy = options["missed_policy"]
        if options["retry_strategy"]:
            task.retry_strategy = options["retry_strategy"]
        if options["retry_interval_seconds"] is not None:
            task.retry_interval_seconds = options["retry_interval_seconds"]

        task.last_run_minute = ""
        self._check_duplicate_task(
            unified_msg_origin=task.unified_msg_origin,
            run_once=False,
            name=task.name,
            reminder=task.reminder,
            cron_expr=task.cron_expr,
            run_at=task.run_at,
            timezone_name=task.timezone_name,
            exclude_task_id=task.task_id,
        )
        await self._sync_task_to_future_list(task)
        await self._save_state()
        return f"任务 #{task_id} 已更新。"

    async def _update_once_task(self, payload: str) -> str:
        parts = self._split_payload(payload)
        if len(parts) < 4:
            raise ValueError(
                "[E_PARAM] 修改单次格式应为：任务ID | 任务名 | 执行时间 | 提醒内容 | 可选重试次数（不改填 -） | 可选参数"
            )

        task_id = self._safe_int(parts[0], "任务 ID")
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")
        if not task.run_once:
            raise ValueError("[E_PARAM] 该任务是周期任务，请使用 `/cron 修改 ...`")

        new_name, new_time, new_reminder = parts[1], parts[2], parts[3]
        options = self._parse_task_options(parts[4:], for_update=True)
        target_timezone = options["timezone_name"] or task.timezone_name

        if new_name != "-":
            if not new_name:
                raise ValueError("[E_PARAM] 任务名不能为空")
            task.name = new_name

        if new_time != "-":
            task.run_at = self._parse_run_at(new_time, target_timezone).isoformat()
        elif options["timezone_name"]:
            # 未修改时间文本时，保持同一触发时刻并切换展示时区。
            task.run_at = self._parse_run_at(task.run_at, target_timezone).isoformat()

        if new_reminder != "-":
            if not new_reminder:
                raise ValueError("[E_PARAM] 提醒内容不能为空")
            task.reminder = new_reminder
        if options["retry_times"] is not None:
            task.retry_times = options["retry_times"]
        if options["timezone_name"]:
            task.timezone_name = options["timezone_name"]
        if options["missed_policy"]:
            task.missed_policy = options["missed_policy"]
        if options["retry_strategy"]:
            task.retry_strategy = options["retry_strategy"]
        if options["retry_interval_seconds"] is not None:
            task.retry_interval_seconds = options["retry_interval_seconds"]

        task.last_run_minute = ""
        self._check_duplicate_task(
            unified_msg_origin=task.unified_msg_origin,
            run_once=True,
            name=task.name,
            reminder=task.reminder,
            cron_expr=task.cron_expr,
            run_at=task.run_at,
            timezone_name=task.timezone_name,
            exclude_task_id=task.task_id,
        )
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
            next_run = (
                t.run_at
                if t.run_once
                else self._format_dt(self._next_cron_time(t.cron_expr, t.timezone_name))
            )
            lines.append(
                f"#{t.task_id} [{kind}/{status}] {t.name} | {schedule} | TZ:{t.timezone_name} | 下次:{next_run or '未知'} | "
                f"重试:{t.retry_times}/{t.retry_interval_seconds}s/{t.retry_strategy} | 错过:{t.missed_policy} | {health} | future:{future_status}"
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
            f"时区：{task.timezone_name}\n"
            f"错过策略：{task.missed_policy}\n"
            f"重试：{task.retry_times} 次，间隔 {task.retry_interval_seconds}s，策略 {task.retry_strategy}\n"
            f"上次执行：{task.last_run_at or '无'}\n"
            f"上次错误：{task.last_error or '无'}\n"
            f"future_job_id：{task.future_job_id or '无'}"
        )

    async def _cron_run_now(self, task_id: int, event: AstrMessageEvent) -> str:
        task = self.tasks.get(task_id)
        if not task or task.unified_msg_origin != event.unified_msg_origin:
            raise ValueError(f"[E_NOT_FOUND] 任务 #{task_id} 不存在")

        ok = await self._execute_task(
            task, self._now_in_timezone(task.timezone_name), reason="manual"
        )
        await self._save_state()
        if ok:
            if task.run_once:
                await self._delete_task(task.task_id)
                return f"单次任务 #{task_id} 已执行并移除。"
            return f"任务 #{task_id} 已立即执行。"
        return f"任务 #{task_id} 执行失败：{task.last_error or '未知错误'}"

    async def _scheduler_loop(self):
        while True:
            try:
                changed = False

                to_delete: list[int] = []
                for task in list(self.tasks.values()):
                    if not task.enabled:
                        continue

                    # 已同步到 AstrBot Future Task，由平台侧调度，避免双重提醒。
                    if task.future_job_id and self._get_cron_manager() is not None:
                        continue

                    task_now = self._now_in_timezone(task.timezone_name)
                    now_minute_key = task_now.strftime("%Y-%m-%d %H:%M")

                    if task.run_once:
                        if task.last_run_minute == now_minute_key:
                            continue
                        run_at_dt = self._parse_run_at(task.run_at, task.timezone_name)
                        if task_now >= run_at_dt:
                            # 单次任务在服务短暂停机后可能错过触发点，这里按 missed_policy 决定补执行或跳过。
                            missed_seconds = int((task_now - run_at_dt).total_seconds())
                            if missed_seconds > 60 and task.missed_policy == "skip":
                                task.last_run_at = task_now.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                                task.last_error = f"跳过执行：错过触发时间约 {missed_seconds // 60} 分钟"
                                task.last_run_minute = now_minute_key
                                to_delete.append(task.task_id)
                                changed = True
                                continue

                            ok = await self._execute_task(task, task_now, reason="once")
                            task.last_run_minute = now_minute_key
                            changed = True
                            if ok:
                                to_delete.append(task.task_id)
                        continue

                    if task.last_run_minute == now_minute_key:
                        continue

                    should_run = self._cron_match(task.cron_expr, task_now)
                    if (
                        not should_run
                        and task.missed_policy == "catch_up"
                        and self._has_missed_cron_between(task, task_now)
                    ):
                        # 周期任务补偿：若上次检查到本次检查之间存在命中点，则补执行一次。
                        should_run = True

                    if should_run:
                        ok = await self._execute_task(task, task_now, reason="cron")
                        task.last_run_minute = now_minute_key
                        if not ok and not task.last_error:
                            task.last_error = "执行失败"
                        changed = True

                    check_key = task_now.strftime("%Y-%m-%d %H:%M")
                    if task.last_check_at != check_key:
                        task.last_check_at = check_key
                        changed = True

                for task_id in to_delete:
                    if task_id in self.tasks:
                        del self.tasks[task_id]
                        changed = True

                if changed:
                    await self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 调度层兜底，确保单次异常不会导致整个调度任务退出。
                logger.exception("[tschedule] scheduler loop error: %s", e)

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
                    await asyncio.sleep(self._retry_wait_seconds(task, attempt))

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
            "retry_strategy": task.retry_strategy,
            "retry_interval_seconds": task.retry_interval_seconds,
            "missed_policy": task.missed_policy,
            "origin": "plugin",
            "plugin": "tschedule",
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
            logger.warning(
                "[tschedule] sync future task failed for #%s: %s", task.task_id, e
            )

    async def _delete_task_from_future_list(self, task: CronTask):
        cron_mgr = self._get_cron_manager()
        if cron_mgr is None or not task.future_job_id:
            return
        try:
            await cron_mgr.delete_job(task.future_job_id)
            task.future_job_id = ""
        except Exception as e:
            logger.warning(
                "[tschedule] delete future task failed for #%s: %s", task.task_id, e
            )

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

    def _parse_run_at(self, text: str, timezone_name: Optional[str] = None) -> datetime:
        value = text.strip()
        if not value:
            raise ValueError("[E_PARAM] 执行时间不能为空")
        tz = self._resolve_timezone(timezone_name or self._default_timezone())
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.astimezone(tz)
            return dt
        except Exception:
            pass

        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=tz)
        except Exception as e:
            raise ValueError(
                "[E_PARAM] 执行时间格式错误，请使用 `YYYY-MM-DD HH:MM` 或 ISO datetime"
            ) from e

    def _validate_cron_expr(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("[E_CRON] cron 表达式必须是 5 段：分 时 日 月 周")

        labels = ["分钟", "小时", "日", "月", "周"]
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        for i, f in enumerate(fields):
            self._parse_cron_field(f, ranges[i][0], ranges[i][1], labels[i])

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
        labels = ["分钟", "小时", "日", "月", "周"]

        for field, value, (min_v, max_v), label in zip(fields, values, ranges, labels):
            allowed = self._parse_cron_field(field, min_v, max_v, label)
            if value not in allowed:
                return False
        return True

    def _parse_cron_field(
        self, field: str, min_v: int, max_v: int, field_label: str = "字段"
    ) -> set:
        result = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"[E_CRON] cron {field_label}字段 `{field}` 非法")

            if part == "*":
                result.update(range(min_v, max_v + 1))
                continue

            if "/" in part:
                base, step_str = part.split("/", 1)
                step = self._safe_int(step_str, f"cron 步长 `{part}`")
                if step <= 0:
                    raise ValueError(
                        f"[E_CRON] cron {field_label}步长必须大于 0：`{part}`"
                    )

                if base == "*":
                    start, end = min_v, max_v
                elif "-" in base:
                    start_str, end_str = base.split("-", 1)
                    start = self._safe_int(start_str, f"cron 范围 `{part}`")
                    end = self._safe_int(end_str, f"cron 范围 `{part}`")
                else:
                    start = self._safe_int(base, f"cron 字段 `{part}`")
                    end = max_v

                self._check_range(
                    start, min_v, max_v, f"cron {field_label}字段 `{part}`"
                )
                self._check_range(end, min_v, max_v, f"cron {field_label}字段 `{part}`")
                if start > end:
                    raise ValueError(
                        f"[E_CRON] cron {field_label}范围起始不能大于结束：`{part}`"
                    )

                result.update(range(start, end + 1, step))
                continue

            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start = self._safe_int(start_str, f"cron 范围 `{part}`")
                end = self._safe_int(end_str, f"cron 范围 `{part}`")
                self._check_range(
                    start, min_v, max_v, f"cron {field_label}范围 `{part}`"
                )
                self._check_range(end, min_v, max_v, f"cron {field_label}范围 `{part}`")
                if start > end:
                    raise ValueError(
                        f"[E_CRON] cron {field_label}范围起始不能大于结束：`{part}`"
                    )
                result.update(range(start, end + 1))
                continue

            value = self._safe_int(part, f"cron 字段 `{part}`")
            self._check_range(value, min_v, max_v, f"cron {field_label}字段 `{part}`")
            result.add(value)

        if not result:
            raise ValueError(f"[E_CRON] cron {field_label}字段 `{field}` 解析后为空")
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
        ids.update(self._extract_ids_from_value(cfg_ids))

        # 兼容 AstrBot 全局管理员配置，避免重复维护两套管理员列表
        try:
            global_cfg = self.context.get_config()
            ids.update(self._extract_admin_ids_from_global_config(global_cfg))
        except Exception:
            pass

        return ids

    def _extract_admin_ids_from_global_config(self, global_cfg: Any) -> set[str]:
        ids: set[str] = set()
        if not isinstance(global_cfg, dict):
            return ids

        # 兼容 AstrBot 不同版本/不同部署常见管理员键名。
        for key in (
            "admins_id",
            "admin_ids",
            "admins",
            "superusers",
            "owners",
            "admin",
        ):
            if key in global_cfg:
                ids.update(self._extract_ids_from_value(global_cfg.get(key)))

        # 一些部署会把权限配置放在嵌套节点中。
        for nested_key in ("platform", "permissions", "security", "bot"):
            nested = global_cfg.get(nested_key)
            if isinstance(nested, dict):
                for key in (
                    "admins_id",
                    "admin_ids",
                    "admins",
                    "superusers",
                    "owners",
                    "admin",
                ):
                    if key in nested:
                        ids.update(self._extract_ids_from_value(nested.get(key)))

        return ids

    def _extract_ids_from_value(self, value: Any) -> set[str]:
        ids: set[str] = set()
        if value is None:
            return ids

        if isinstance(value, (list, tuple, set)):
            for item in value:
                s = str(item).strip()
                if s:
                    ids.add(s)
            return ids

        if isinstance(value, dict):
            # 尽量从结构化对象中提取可识别身份字段。
            for k in ("id", "uid", "user_id", "value", "name", "username"):
                v = value.get(k)
                if v is not None:
                    s = str(v).strip()
                    if s:
                        ids.add(s)
            return ids

        text = str(value).strip()
        if not text:
            return ids
        # 兼容 "id1,id2" / "id1 id2" / "id1|id2" 等文本输入。
        for token in re.split(r"[,\s;|]+", text):
            s = token.strip()
            if s:
                ids.add(s)
        return ids

    def _normalize_admin_text(self, text: str) -> str:
        s = str(text).strip().lower()
        # 常见分隔/前缀符号统一去除，提升关键词匹配容错
        for ch in (" ", "\t", "\n", "-", "_", ":", "|", "@"):
            s = s.replace(ch, "")
        return s

    def _admin_match_candidates(self, event: AstrMessageEvent) -> list[str]:
        candidates = []

        actor = self._actor_id(event)
        if actor:
            candidates.append(actor)

        sender = getattr(event, "sender", None)
        if sender is not None:
            for key in ("id", "user_id", "uid", "nickname", "name", "username"):
                value = getattr(sender, key, None)
                if value is not None:
                    candidates.append(str(value))

        for key in ("sender_id", "user_id", "userId", "uid"):
            value = getattr(event, key, None)
            if value is not None:
                candidates.append(str(value))

        return candidates

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        # 平台如果已经标记管理员身份，直接放行
        for key in ("is_admin", "is_superuser", "is_owner"):
            if bool(getattr(event, key, False)):
                return True
        sender = getattr(event, "sender", None)
        if sender is not None:
            for key in ("is_admin", "is_superuser", "is_owner"):
                if bool(getattr(sender, key, False)):
                    return True

        admin_keywords = [
            self._normalize_admin_text(x) for x in self._admin_ids() if str(x).strip()
        ]
        if not admin_keywords:
            return False

        candidates = [
            self._normalize_admin_text(x) for x in self._admin_match_candidates(event)
        ]
        for keyword in admin_keywords:
            if not keyword:
                continue
            for cand in candidates:
                if cand == keyword or keyword in cand:
                    return True
        return False

    def _admin_only_enabled(self) -> bool:
        return bool(self._config_get("admin_only_cron", True))

    def _default_retry_times(self) -> int:
        v = self._config_get("default_retry_times", 0)
        try:
            v = int(v)
        except Exception:
            v = 0
        return max(0, v)

    def _default_retry_interval_seconds(self) -> int:
        v = self._config_get("default_retry_interval_seconds", 2)
        try:
            v = int(v)
        except Exception:
            v = 2
        return max(1, v)

    def _default_retry_strategy(self) -> str:
        return self._normalize_retry_strategy(
            self._config_get("default_retry_strategy", "fixed")
        )

    def _default_timezone(self) -> str:
        tz = (
            str(self._config_get("default_timezone", "Asia/Shanghai")).strip()
            or "Asia/Shanghai"
        )
        self._resolve_timezone(tz)
        return tz

    def _default_missed_policy(self) -> str:
        return self._normalize_missed_policy(
            self._config_get("default_missed_policy", "catch_up")
        )

    def _session_task_limit(self) -> int:
        v = self._config_get("session_task_limit", 50)
        try:
            v = int(v)
        except Exception:
            v = 50
        return max(1, v)

    def _duplicate_check_enabled(self) -> bool:
        return bool(self._config_get("duplicate_check", True))

    def _catch_up_scan_limit_minutes(self) -> int:
        v = self._config_get("catch_up_scan_limit_minutes", 180)
        try:
            v = int(v)
        except Exception:
            v = 180
        return min(1440, max(10, v))

    def _normalize_missed_policy(self, raw: Any) -> str:
        value = str(raw or "").strip().lower()
        if value in {"catch_up", "catchup", "补执行", "补跑", "补偿"}:
            return "catch_up"
        if value in {"skip", "跳过"}:
            return "skip"
        raise ValueError("[E_PARAM] 错过策略仅支持 catch_up/补执行 或 skip/跳过")

    def _normalize_retry_strategy(self, raw: Any) -> str:
        value = str(raw or "").strip().lower()
        if value in {"fixed", "固定"}:
            return "fixed"
        if value in {"exponential", "指数", "指数退避"}:
            return "exponential"
        raise ValueError("[E_PARAM] 重试策略仅支持 fixed/固定 或 exponential/指数")

    def _resolve_timezone(self, timezone_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(str(timezone_name).strip())
        except Exception as e:
            raise ValueError(
                f"[E_PARAM] 时区无效：`{timezone_name}`，例如 `Asia/Shanghai`"
            ) from e

    def _now_in_timezone(self, timezone_name: str) -> datetime:
        return datetime.now(self._resolve_timezone(timezone_name))

    def _parse_task_options(
        self, raw_parts: list[str], for_update: bool = False
    ) -> Dict[str, Any]:
        retry_times = None if for_update else self._default_retry_times()
        timezone_name = None if for_update else self._default_timezone()
        missed_policy = None if for_update else self._default_missed_policy()
        retry_strategy = None if for_update else self._default_retry_strategy()
        retry_interval_seconds = (
            None if for_update else self._default_retry_interval_seconds()
        )

        pending_tokens = []
        for token in raw_parts:
            t = token.strip()
            if not t:
                continue
            if t == "-":
                continue
            pending_tokens.append(t)

        for token in pending_tokens:
            # 支持 key=value / key:value / key：value 三种写法，降低命令输入门槛。
            if "=" in token:
                key, value = token.split("=", 1)
            elif "：" in token:
                key, value = token.split("：", 1)
            elif ":" in token:
                key, value = token.split(":", 1)
            else:
                key, value = "", token

            key = key.strip().lower()
            value = value.strip()

            if not key:
                # 兼容旧格式：第 4 段直接写数字时视作 retry_times。
                if re.fullmatch(r"-?\d+", value):
                    rv = self._safe_int(value, "重试次数")
                    if rv < 0:
                        raise ValueError("[E_PARAM] 重试次数不能为负数")
                    retry_times = rv
                    continue
                lowered = value.lower()
                if lowered in {
                    "catch_up",
                    "catchup",
                    "补执行",
                    "补跑",
                    "补偿",
                    "skip",
                    "跳过",
                }:
                    missed_policy = self._normalize_missed_policy(value)
                    continue
                if lowered in {"fixed", "固定", "exponential", "指数", "指数退避"}:
                    retry_strategy = self._normalize_retry_strategy(value)
                    continue
                raise ValueError(
                    f"[E_PARAM] 无法识别可选参数：`{value}`，支持如 retry=2 | tz=Asia/Shanghai | miss=skip"
                )

            if key in {"retry", "retry_times", "重试", "重试次数"}:
                rv = self._safe_int(value, "重试次数")
                if rv < 0:
                    raise ValueError("[E_PARAM] 重试次数不能为负数")
                retry_times = rv
                continue

            if key in {"tz", "timezone", "时区"}:
                self._resolve_timezone(value)
                timezone_name = value
                continue

            if key in {"miss", "missed", "missed_policy", "错过策略"}:
                missed_policy = self._normalize_missed_policy(value)
                continue

            if key in {"retry_strategy", "重试策略"}:
                retry_strategy = self._normalize_retry_strategy(value)
                continue

            if key in {
                "retry_interval",
                "retry_interval_seconds",
                "重试间隔",
                "重试间隔秒",
            }:
                iv = self._safe_int(value, "重试间隔秒数")
                if iv <= 0:
                    raise ValueError("[E_PARAM] 重试间隔秒数必须大于 0")
                retry_interval_seconds = iv
                continue

            raise ValueError(f"[E_PARAM] 未知可选参数：`{key}`")

        return {
            "retry_times": retry_times,
            "timezone_name": timezone_name,
            "missed_policy": missed_policy,
            "retry_strategy": retry_strategy,
            "retry_interval_seconds": retry_interval_seconds,
        }

    def _check_session_task_limit(self, unified_msg_origin: str):
        count = sum(
            1 for t in self.tasks.values() if t.unified_msg_origin == unified_msg_origin
        )
        limit = self._session_task_limit()
        if count >= limit:
            raise ValueError(f"[E_LIMIT] 当前会话任务数已达上限（{limit}）")

    def _check_duplicate_task(
        self,
        unified_msg_origin: str,
        run_once: bool,
        name: str,
        reminder: str,
        cron_expr: str,
        run_at: str,
        timezone_name: str,
        exclude_task_id: Optional[int],
    ):
        if not self._duplicate_check_enabled():
            return
        for task in self.tasks.values():
            if exclude_task_id is not None and task.task_id == exclude_task_id:
                continue
            if task.unified_msg_origin != unified_msg_origin:
                continue
            if task.run_once != run_once:
                continue
            if task.name != name or task.reminder != reminder:
                continue
            if task.timezone_name != timezone_name:
                continue
            if run_once and task.run_at == run_at:
                raise ValueError(
                    "[E_DUPLICATE] 检测到重复单次提醒（同会话/同名称/同时间/同内容）"
                )
            if (not run_once) and task.cron_expr == cron_expr:
                raise ValueError(
                    "[E_DUPLICATE] 检测到重复 cron 任务（同会话/同表达式/同内容）"
                )

    def _next_cron_time(self, expr: str, timezone_name: str) -> Optional[datetime]:
        tz = self._resolve_timezone(timezone_name)
        cursor = datetime.now(tz).replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        for _ in range(0, 366 * 24 * 60):
            if self._cron_match(expr, cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return None

    def _format_dt(self, dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    def _retry_wait_seconds(self, task: CronTask, attempt: int) -> int:
        base = max(1, int(task.retry_interval_seconds))
        if task.retry_strategy == "exponential":
            return min(300, base * (2 ** (attempt - 1)))
        return min(300, base)

    def _parse_minute_key(self, text: str, timezone_name: str) -> Optional[datetime]:
        if not text:
            return None
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=self._resolve_timezone(timezone_name))
        except Exception:
            return None

    def _has_missed_cron_between(self, task: CronTask, now_dt: datetime) -> bool:
        last_checked = self._parse_minute_key(task.last_check_at, task.timezone_name)
        if last_checked is None:
            return False

        current_minute = now_dt.replace(second=0, microsecond=0)
        if current_minute <= last_checked:
            return False

        gap_minutes = int((current_minute - last_checked).total_seconds() // 60)
        if gap_minutes <= 1:
            return False

        scan_limit = self._catch_up_scan_limit_minutes()
        if gap_minutes > scan_limit:
            # 超过扫描上限时，保守补执行一次，避免长时间离线后完全漏提醒。
            return True

        cursor = last_checked + timedelta(minutes=1)
        while cursor < current_minute:
            if self._cron_match(task.cron_expr, cursor):
                return True
            cursor += timedelta(minutes=1)
        return False

    def _ensure_admin(self, event: AstrMessageEvent):
        if not self._admin_only_enabled():
            return
        if self._is_admin(event):
            return
        raise PermissionError("[E_FORBIDDEN] 当前插件配置为仅管理员可操作 cron 任务")

    def _cron_help_text(self) -> str:
        return (
            "cron 管理（v2.1.0）\n"
            "1) /cron 添加 任务名 | */5 * * * * | 提醒内容 | 可选重试次数 | 可选参数\n"
            "2) /cron 单次 任务名 | 2026-04-05 09:30 | 提醒内容 | 可选重试次数 | 可选参数\n"
            "   说明：原 todo 已并入单次提醒\n"
            "3) /cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容 | 可选重试次数 | 可选参数\n"
            "4) /cron 修改单次 任务ID | 新任务名 | 新执行时间 | 新提醒内容 | 可选重试次数 | 可选参数\n"
            "5) /cron 删除 任务ID\n"
            "6) /cron 启用 任务ID\n"
            "7) /cron 禁用 任务ID\n"
            "8) /cron 列表\n"
            "9) /cron 日志 任务ID\n"
            "10) /cron 立即执行 任务ID\n"
            "\n"
            "可选参数示例：tz=Asia/Shanghai | miss=catch_up(或skip) | retry_strategy=fixed(或exponential) | retry_interval=5\n"
            "创建后会回显“下次触发时间预览”。\n"
            "\n"
            "自然语言识别已交给主助手，请由助手调用工具：\n"
            "- create_cron_task（周期）\n"
            "- create_once_reminder（单次）\n"
            "管理员控制：默认读取 AstrBot 全局管理员；也可在插件 WebUI 的 admin_ids 追加关键词管理员。"
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
                logger.exception("[tschedule] load local state failed: %s", e)

        # 兼容历史本地文件名（collect_skill）
        if cron_data is None and self._legacy_local_store_path_v2.exists():
            try:
                obj = json.loads(
                    self._legacy_local_store_path_v2.read_text(encoding="utf-8")
                )
                cron_data = obj.get("cron_tasks", [])
                self.next_task_id = int(obj.get("cron_next_id", 1))
            except Exception as e:
                logger.exception("[tschedule] load legacy v2 local state failed: %s", e)

        if cron_data is None and self._legacy_local_store_path.exists():
            try:
                obj = json.loads(
                    self._legacy_local_store_path.read_text(encoding="utf-8")
                )
                cron_data = obj.get("tasks", [])
                self.next_task_id = int(obj.get("next_id", 1))
                migrated_from_v1 = True
            except Exception as e:
                logger.exception("[tschedule] load local v1 state failed: %s", e)

        cron_data = cron_data or []

        self.tasks = self._deserialize_cron_tasks(cron_data)
        self.next_task_id = max(
            self.next_task_id, max(self.tasks.keys(), default=0) + 1
        )

        if migrated_from_v1:
            logger.info("[tschedule] detected v1 data, writing migrated v2 store")
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
                    retry_strategy=str(item.get("retry_strategy", "fixed")),
                    retry_interval_seconds=max(
                        1, int(item.get("retry_interval_seconds", 2))
                    ),
                    missed_policy=str(item.get("missed_policy", "catch_up")),
                    last_run_minute=str(item.get("last_run_minute", "")),
                    last_check_at=str(item.get("last_check_at", "")),
                    last_error=str(item.get("last_error", "")),
                    last_run_at=str(item.get("last_run_at", "")),
                    future_job_id=str(item.get("future_job_id", "")),
                )
                try:
                    task.retry_strategy = self._normalize_retry_strategy(
                        task.retry_strategy
                    )
                except Exception:
                    task.retry_strategy = "fixed"
                try:
                    task.missed_policy = self._normalize_missed_policy(
                        task.missed_policy
                    )
                except Exception:
                    task.missed_policy = "catch_up"
                if (
                    task.task_id > 0
                    and task.name
                    and task.reminder
                    and task.unified_msg_origin
                ):
                    if task.run_once:
                        if task.run_at:
                            tasks[task.task_id] = task
                    else:
                        if task.cron_expr:
                            tasks[task.task_id] = task
            except Exception as e:
                logger.warning(
                    "[tschedule] skip invalid cron task item: %s err=%s", item, e
                )
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
