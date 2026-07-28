from assistant.router.handlers.applications import ApplicationWindowHandler
from assistant.router.handlers.applications_launch import ApplicationHandler
from assistant.router.handlers.control import ControlHandler, MemoryConversationHandler
from assistant.router.handlers.devices import DeviceStatusHandler
from assistant.router.handlers.devices_control import DeviceControlHandler
from assistant.router.handlers.fallback import FallbackHandler
from assistant.router.handlers.files import FileMutationHandler, FileSearchHandler
from assistant.router.handlers.files_operation import FileOperationHandler
from assistant.router.handlers.media import MediaHandler
from assistant.router.handlers.network import NetworkHandler
from assistant.router.handlers.productivity import ProductivityHandler
from assistant.router.types import RouteHandler

DEFAULT_ROUTE_HANDLERS: tuple[RouteHandler, ...] = (
    ControlHandler(),
    MemoryConversationHandler(),
    ProductivityHandler(),
    NetworkHandler(),
    DeviceStatusHandler(),
    FileSearchHandler(),
    FileMutationHandler(),
    MediaHandler(),
    DeviceControlHandler(),
    ApplicationWindowHandler(),
    FileOperationHandler(),
    ApplicationHandler(),
    FallbackHandler(),
)
