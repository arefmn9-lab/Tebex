from enum import Enum


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"
    DISABLED = "disabled"
    STARTING = "starting"
    STOPPING = "stopping"
