"""数控实训步骤依赖流程领域。"""

from app.cnc.repository import ensure_schema
from app.cnc.service import CncFlowService

__all__ = ["CncFlowService", "ensure_schema"]
