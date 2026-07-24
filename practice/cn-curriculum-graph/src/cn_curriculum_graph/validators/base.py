"""校验结果的公共类型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    ERROR = "error"      # CI 必须失败
    WARNING = "warning"  # 记录但放行


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Severity
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
