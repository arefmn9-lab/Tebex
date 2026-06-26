from enum import Enum


class PriorityLevel(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
