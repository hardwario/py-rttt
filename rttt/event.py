import enum


@enum.unique
class EventType(enum.Enum):
    OPEN = 'open'
    CLOSE = 'close'
    OUT = 'out'  # terminal line out
    IN = 'in'    # terminal line in
    LOG = 'log'  # logger line out


class Event:
    def __init__(self, type: EventType, data):
        self.type = type
        self.data = data
