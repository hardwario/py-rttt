import enum


@enum.unique
class EventType(enum.Enum):
    # OPEN and CLOSE are deprecated: they were only ever emitted, never
    # consumed, and the session lifecycle they describe is already carried by
    # the open()/close() calls themselves. Use CONN to observe a transport
    # coming up or going away. Still emitted for backwards compatibility.
    OPEN = 'open'
    CLOSE = 'close'
    OUT = 'out'      # terminal line out
    IN = 'in'        # terminal line in
    LOG = 'log'      # logger line out
    FLASH = 'flash'  # flash programming event
    CONN = 'conn'    # transport connected or disconnected


class Event:
    def __init__(self, type: EventType, data):
        self.type = type
        self.data = data


def conn_event(source: str, status: str, error: str = '') -> Event:
    """Build a CONN event.

    A chain can hold more than one transport — an MQTT bridge wrapping an RTT
    connector, say — so `source` names whose connection changed and consumers
    can track them independently.

    Args:
        source: transport identifier, e.g. 'rtt' or 'mqtt'.
        status: 'connected', 'disconnected' or 'connecting'.
        error: reason the transport went away, when there is one.
    """
    return Event(EventType.CONN, {'source': source, 'status': status, 'error': error})
