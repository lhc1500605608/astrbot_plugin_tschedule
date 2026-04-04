import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


KV_KEY_TASKS = "cron_tasks_v1"
KV_KEY_NEXT_ID = "cron_next_id_v1"


@dataclass
class CronTask:
    task_id: int
    name: str
    cron_expr: str
    reminder: str
    unified_msg_origin: str
    timezone_name: str
    enabled: bool = True
    last_run_minute: str = ""


@register("collect_skill", "Tango", "AstrBot 技能汇总（v1: cron 任务管理）", "1.0.0")
class CollectSkillPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.tasks: Dict[int, CronTask] = {}
        self.next_id: int = 1
        self._scheduler_task: Optional[asyncio.Task] = None
        self._local_store_path = Path(__file__).resolve().parent / ".cron_tasks_v1.json"

    async def initialize(self):
        await self._load_state()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[collect_skill] cron scheduler started")

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    @filter.command("cron", alias={"定时", "cron任务"})
    async def cron_command(self, event: AstrMessageEvent):
        """Cron 任务管理。格式：/cron 添加 任务名 | */5 * * * * | 提醒内容"""
        raw = event.message_str.strip()
        body = ""
        if raw.startswith("/"):
            parts = raw[1:].split(" ", 1)
            body = parts[1].strip() if len(parts) > 1 else ""
        else:
            parts = raw.split(" ", 1)
            body = parts[1].strip() if len(parts) > 1 else ""
        if not body or body in {"help", "帮助", "?", "-h", "--help"}:
            yield event.plain_result(self._help_text())
            return

        try:
            resp = await self._handle_text_command(body, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] /cron command failed: %s", e)
            yield event.plain_result(f"处理失败：{self._friendly_error(e)}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cron_keyword_command(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text or text.startswith("/"):
            return

        if not self._looks_like_keyword_command(text):
            return

        try:
            resp = await self._handle_text_command(text, event)
            if resp:
                yield event.plain_result(resp)
        except Exception as e:
            logger.exception("[collect_skill] keyword command failed: %s", e)
            yield event.plain_result(f"处理失败：{self._friendly_error(e)}")

    async def _handle_text_command(self, body: str, event: AstrMessageEvent) -> str:
        normalized = body.strip()

        if normalized.startswith("添加 ") or normalized.startswith("创建 "):
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event)

        if normalized.startswith("修改 ") or normalized.startswith("更新 "):
            payload = normalized.split(" ", 1)[1].strip()
            return await self._update_task(payload)

        if normalized.startswith("删除 ") or normalized.startswith("移除 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._delete_task(task_id)

        if normalized.startswith("启用 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._toggle_task(task_id, True)

        if normalized.startswith("禁用 "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._toggle_task(task_id, False)

        if normalized in {"列表", "查看", "查看任务", "列出", "list", "ls"}:
            return self._list_tasks(event.unified_msg_origin)

        if normalized in {"help", "帮助", "?", "-h", "--help"}:
            return self._help_text()

        # 关键词模式：创建cron ... / 修改cron ...
        if normalized.startswith("创建cron ") or normalized.startswith("新建cron "):
            payload = normalized.split(" ", 1)[1].strip()
            return await self._create_task(payload, event)

        if normalized.startswith("修改cron ") or normalized.startswith("更新cron "):
            payload = normalized.split(" ", 1)[1].strip()
            return await self._update_task(payload)

        if normalized.startswith("删除cron "):
            task_id = self._safe_int(normalized.split(" ", 1)[1].strip(), "任务 ID")
            return await self._delete_task(task_id)

        if normalized in {"查看cron", "列出cron", "cron列表"}:
            return self._list_tasks(event.unified_msg_origin)

        return (
            "没有识别到可执行的 cron 指令。\n"
            "输入 `/cron 帮助` 查看格式，或使用关键词：`创建cron` / `修改cron` / `删除cron` / `查看cron`。"
        )

    async def _create_task(self, payload: str, event: AstrMessageEvent) -> str:
        parts = self._split_payload(payload)
        if len(parts) != 3:
            raise ValueError("创建格式不正确，应为：任务名 | cron表达式 | 提醒内容")

        name, cron_expr, reminder = parts
        if not name:
            raise ValueError("任务名不能为空")
        if not reminder:
            raise ValueError("提醒内容不能为空")

        self._validate_cron_expr(cron_expr)

        task = CronTask(
            task_id=self.next_id,
            name=name,
            cron_expr=cron_expr,
            reminder=reminder,
            unified_msg_origin=event.unified_msg_origin,
            timezone_name="Asia/Shanghai",
            enabled=True,
        )

        self.tasks[task.task_id] = task
        self.next_id += 1
        await self._save_state()

        return (
            f"已创建 cron 任务 #{task.task_id}\n"
            f"名称：{task.name}\n"
            f"表达式：{task.cron_expr}\n"
            f"提醒：{task.reminder}"
        )

    async def _update_task(self, payload: str) -> str:
        parts = self._split_payload(payload)
        if len(parts) != 4:
            raise ValueError("修改格式不正确，应为：任务ID | 任务名 | cron表达式 | 提醒内容（不修改可填 -）")

        task_id = self._safe_int(parts[0], "任务 ID")
        if task_id not in self.tasks:
            raise ValueError(f"任务 #{task_id} 不存在")

        task = self.tasks[task_id]
        new_name, new_cron, new_reminder = parts[1], parts[2], parts[3]

        if new_name != "-":
            if not new_name:
                raise ValueError("任务名不能为空")
            task.name = new_name

        if new_cron != "-":
            self._validate_cron_expr(new_cron)
            task.cron_expr = new_cron

        if new_reminder != "-":
            if not new_reminder:
                raise ValueError("提醒内容不能为空")
            task.reminder = new_reminder

        task.last_run_minute = ""
        await self._save_state()
        return f"任务 #{task_id} 已更新。"

    async def _delete_task(self, task_id: int) -> str:
        if task_id not in self.tasks:
            raise ValueError(f"任务 #{task_id} 不存在")
        del self.tasks[task_id]
        await self._save_state()
        return f"任务 #{task_id} 已删除。"

    async def _toggle_task(self, task_id: int, enabled: bool) -> str:
        if task_id not in self.tasks:
            raise ValueError(f"任务 #{task_id} 不存在")
        task = self.tasks[task_id]
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
            lines.append(
                f"#{t.task_id} [{status}] {t.name} | {t.cron_expr} | {t.reminder}"
            )
        return "\n".join(lines)

    async def _scheduler_loop(self):
        while True:
            now = datetime.now()
            now_key = now.strftime("%Y-%m-%d %H:%M")
            for task in list(self.tasks.values()):
                if not task.enabled:
                    continue
                if task.last_run_minute == now_key:
                    continue

                try:
                    if self._cron_match(task.cron_expr, now):
                        await self._send_task_notification(task)
                        task.last_run_minute = now_key
                        await self._save_state()
                except Exception as e:
                    logger.exception("[collect_skill] run cron task #%s failed: %s", task.task_id, e)
            await asyncio.sleep(20)

    async def _send_task_notification(self, task: CronTask):
        # 使用主动消息发送到任务创建时的会话。
        try:
            from astrbot.api.event import MessageChain

            chain = MessageChain().message(
                f"[cron提醒 #{task.task_id}] {task.name}\n{task.reminder}\n({task.cron_expr})"
            )
            await self.context.send_message(task.unified_msg_origin, chain)
        except Exception:
            # 向后兼容：如果 MessageChain 构造失败，则尝试使用字符串。
            await self.context.send_message(
                task.unified_msg_origin,
                f"[cron提醒 #{task.task_id}] {task.name}\n{task.reminder}\n({task.cron_expr})",
            )

    def _validate_cron_expr(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError("cron 表达式必须是 5 段：分 时 日 月 周")

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
            (now.weekday() + 1) % 7,  # Python Monday=0, cron Sunday=0
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
                raise ValueError(f"cron 字段 `{field}` 非法")

            if part == "*":
                result.update(range(min_v, max_v + 1))
                continue

            if "/" in part:
                base, step_str = part.split("/", 1)
                step = self._safe_int(step_str, f"cron 步长 `{part}`")
                if step <= 0:
                    raise ValueError(f"cron 步长必须大于 0：`{part}`")

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
                    raise ValueError(f"cron 范围起始不能大于结束：`{part}`")

                result.update(range(start, end + 1, step))
                continue

            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start = self._safe_int(start_str, f"cron 范围 `{part}`")
                end = self._safe_int(end_str, f"cron 范围 `{part}`")
                self._check_range(start, min_v, max_v, f"cron 范围 `{part}`")
                self._check_range(end, min_v, max_v, f"cron 范围 `{part}`")
                if start > end:
                    raise ValueError(f"cron 范围起始不能大于结束：`{part}`")
                result.update(range(start, end + 1))
                continue

            value = self._safe_int(part, f"cron 字段 `{part}`")
            self._check_range(value, min_v, max_v, f"cron 字段 `{part}`")
            result.add(value)

        if not result:
            raise ValueError(f"cron 字段 `{field}` 解析后为空")
        return result

    def _check_range(self, value: int, min_v: int, max_v: int, label: str):
        if value < min_v or value > max_v:
            raise ValueError(f"{label} 超出范围（{min_v}-{max_v}）")

    def _safe_int(self, text: str, label: str) -> int:
        try:
            return int(text)
        except Exception as e:
            raise ValueError(f"{label} 不是合法整数：`{text}`") from e

    def _friendly_error(self, e: Exception) -> str:
        if isinstance(e, ValueError):
            return str(e)
        return f"未知错误：{str(e)}"

    def _looks_like_keyword_command(self, text: str) -> bool:
        return bool(
            re.match(
                r"^(创建cron|新建cron|修改cron|更新cron|删除cron|查看cron|列出cron|cron列表)\b",
                text,
            )
        )

    def _split_payload(self, payload: str) -> list:
        normalized = payload.replace("｜", "|")
        return [p.strip() for p in normalized.split("|")]

    def _help_text(self) -> str:
        return (
            "cron 管理（v1）\n"
            "1) /cron 添加 任务名 | */5 * * * * | 提醒内容\n"
            "2) /cron 修改 任务ID | 新任务名 | 新cron表达式 | 新提醒内容\n"
            "   不修改的字段填 -\n"
            "3) /cron 删除 任务ID\n"
            "4) /cron 启用 任务ID\n"
            "5) /cron 禁用 任务ID\n"
            "6) /cron 列表\n"
            "\n"
            "关键词也支持：\n"
            "- 创建cron 任务名 | */5 * * * * | 提醒内容\n"
            "- 修改cron 任务ID | 新任务名 | 新cron表达式 | 新提醒内容\n"
            "- 删除cron 任务ID\n"
            "- 查看cron"
        )

    async def _load_state(self):
        data = None

        # 优先用 AstrBot KV。
        try:
            data = await self.get_kv_data(KV_KEY_TASKS, None)
            self.next_id = int(await self.get_kv_data(KV_KEY_NEXT_ID, 1))
        except Exception:
            data = None

        # 兼容本地文件兜底。
        if data is None and self._local_store_path.exists():
            try:
                obj = json.loads(self._local_store_path.read_text(encoding="utf-8"))
                data = obj.get("tasks", [])
                self.next_id = int(obj.get("next_id", 1))
            except Exception as e:
                logger.exception("[collect_skill] load local state failed: %s", e)

        if not data:
            self.tasks = {}
            self.next_id = max(self.next_id, 1)
            return

        tasks: Dict[int, CronTask] = {}
        for item in data:
            try:
                task = CronTask(**item)
                tasks[task.task_id] = task
            except Exception as e:
                logger.warning("[collect_skill] skip invalid task item: %s, err=%s", item, e)

        self.tasks = tasks
        self.next_id = max(self.next_id, max(self.tasks.keys(), default=0) + 1)

    async def _save_state(self):
        payload = [asdict(t) for t in self.tasks.values()]

        kv_ok = False
        try:
            await self.put_kv_data(KV_KEY_TASKS, payload)
            await self.put_kv_data(KV_KEY_NEXT_ID, self.next_id)
            kv_ok = True
        except Exception:
            kv_ok = False

        if not kv_ok:
            obj = {"tasks": payload, "next_id": self.next_id}
            self._local_store_path.write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
