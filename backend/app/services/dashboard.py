"""监控大盘业务规则：状态流转、字段校验与数据服务响应快照消费都收在这里。

大盘是数据服务的下游消费方：发布大盘与刷新大盘只读取已上线数据服务的当前契约，
通过 DataserviceService.collect_published_snapshots 同步消费数据服务响应数据快照；
服务下线后不再返回新结果。大盘侧只读消费，不修改契约与快照（职责归数据服务模块）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.dataservice import DataserviceService
from app.store import store

MODULE = "dashboard"
REQUIRED_FIELDS = ["大盘编号", "大盘名称", "包含指标组"]
STATUS_ORDER = ["草稿", "大盘评审中", "已发布", "已下线"]
ACTION_RULES = {"提交大盘评审": "大盘评审中", "发布大盘": "已发布", "下线大盘": "已下线"}
NEGATIVE_ACTIONS = ["下线大盘"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DashboardService:
    def __init__(self) -> None:
        # 下游取数入口：数据服务模块提供的已发布契约快照消费口
        self._dataservice = DataserviceService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("大盘编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _consume_snapshots(self, entry: dict[str, Any]) -> str:
        """读取已上线服务的当前契约并同步消费响应快照，把消费结果登记到大盘上。"""
        consumed, skipped = self._dataservice.collect_published_snapshots()
        entry["消费快照"] = consumed
        entry["最近取数时间"] = _now()
        message = f"同步消费 {len(consumed)} 份数据服务响应快照"
        if consumed:
            refs = "、".join(
                f"{item['服务编号']}@契约v{item['契约版本']}({item['缓存签名']})"
                for item in consumed
            )
            message += f"：{refs}"
        if skipped:
            reasons = "、".join(f"{item['服务编号']}（{item['原因']}）" for item in skipped)
            message += f"；跳过 {len(skipped)} 个服务：{reasons}"
        return message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"大盘配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于监控大盘可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"大盘配置已{action}"
        if action == "发布大盘":
            # 发布即取数：只读取已上线服务的当前契约，同步消费响应快照
            message += "，" + self._consume_snapshots(entry)
        return entry, message

    def refresh_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """刷新大盘取数：只允许已发布大盘刷新，读取已上线服务的当前契约。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"大盘配置 {entry_id} 不存在或已归档"
        if entry.get("status") != "已发布":
            return None, (
                f"大盘配置当前状态为「{entry.get('status')}」，只有已发布大盘才能刷新取数"
            )
        return entry, "大盘已刷新，" + self._consume_snapshots(entry)
