from assistant.router.handlers.anthropic import AnthropicHandler
from assistant.router.handlers.applications import ApplicationWindowHandler
from assistant.router.handlers.applications_launch import ApplicationHandler
from assistant.router.handlers.calendar import CalendarHandler
from assistant.router.handlers.control import ControlHandler, MemoryConversationHandler
from assistant.router.handlers.devices import DeviceStatusHandler
from assistant.router.handlers.devices_control import DeviceControlHandler
from assistant.router.handlers.fallback import FallbackHandler
from assistant.router.handlers.files import FileMutationHandler, FileSearchHandler
from assistant.router.handlers.files_operation import FileOperationHandler
from assistant.router.handlers.github import GitHubHandler
from assistant.router.handlers.gmail import GmailHandler
from assistant.router.handlers.google_drive import GoogleDriveHandler
from assistant.router.handlers.media import MediaHandler
from assistant.router.handlers.network import NetworkHandler
from assistant.router.handlers.notion import NotionHandler
from assistant.router.handlers.onedrive import OneDriveHandler
from assistant.router.handlers.openai import OpenAIHandler
from assistant.router.handlers.outlook_calendar import OutlookCalendarHandler
from assistant.router.handlers.outlook_mail import OutlookMailHandler
from assistant.router.handlers.productivity import ProductivityHandler
from assistant.router.handlers.slack import SlackHandler
from assistant.router.handlers.visualization import VisualizationHandler
from assistant.router.types import RouteHandler

DEFAULT_ROUTE_HANDLERS: tuple[RouteHandler, ...] = (
    ControlHandler(),
    MemoryConversationHandler(),
    CalendarHandler(),
    GmailHandler(),
    GoogleDriveHandler(),
    OutlookMailHandler(),
    OutlookCalendarHandler(),
    OneDriveHandler(),
    SlackHandler(),
    NotionHandler(),
    GitHubHandler(),
    OpenAIHandler(),
    AnthropicHandler(),
    ProductivityHandler(),
    VisualizationHandler(),
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
