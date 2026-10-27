"""规划输出的契约。模型（或模板）产出的 JSON 必须先过这里，才允许进入执行器。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.tools import registry


class Step(BaseModel):
    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    on_fail: Literal["abort", "skip", "compensate"] = "abort"
    label: str = ""

    @field_validator("tool")
    @classmethod
    def _tool_exists(cls, v: str) -> str:
        if v not in registry.TOOLS:
            raise ValueError(f"工具不在白名单内：{v}")
        return v

    @property
    def is_gated(self) -> bool:
        """是否必须先过权限闸门：会划钱，或不可逆/需审批的动作。"""
        return registry.TOOLS[self.tool].is_gated


class Plan(BaseModel):
    intent: str
    slots: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list, max_length=8)
    missing: list[str] = Field(default_factory=list)
    note: str = ""

    def gated_steps(self) -> list[Step]:
        return [s for s in self.steps if s.is_gated]

    def primary_action(self) -> str | None:
        for s in self.steps:
            spec = registry.TOOLS[s.tool]
            if s.is_gated and spec.action:
                return spec.action
        return None
