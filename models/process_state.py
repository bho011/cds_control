from enum import Enum, auto


class ProcessState(Enum):
    IDLE = auto()
    OPEN_VALVES = auto()
    START_PUMP = auto()
    RUNNING = auto()
    STOP_PUMP = auto()
    CLOSE_VALVES = auto()
    FINISHED = auto()
    ERROR = auto()