from rttt.connectors.base import Connector
from rttt.connectors.middleware import Middleware, AsyncMiddleware, BufferedMiddleware
from rttt.connectors.demo import DemoConnector
from rttt.connectors.file_log import FileLogMiddleware
from rttt.connectors.mcp_server import MCPMiddleware, MCPPortInUseError
from rttt.connectors.pylink_rtt import PyLinkRTTConnector
from rttt.connectors.substitution import SubstitutionMiddleware

# Backward compatibility alias (deprecated, use FileLogMiddleware)
FileLogConnector = FileLogMiddleware

# Re-exported for `from rttt.connectors import ...`, which is how everything
# outside this package reaches them.
__all__ = [
    'AsyncMiddleware',
    'BufferedMiddleware',
    'Connector',
    'DemoConnector',
    'FileLogConnector',
    'FileLogMiddleware',
    'MCPMiddleware',
    'MCPPortInUseError',
    'Middleware',
    'PyLinkRTTConnector',
    'SubstitutionMiddleware',
]
