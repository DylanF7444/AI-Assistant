"""Extension discovery and installation services."""

from assistant.extensions.platform import (
    CatalogExtension,
    ExtensionCatalog,
    ExtensionHubService,
    ExtensionPlatformClient,
)

__all__ = (
    "CatalogExtension",
    "ExtensionCatalog",
    "ExtensionHubService",
    "ExtensionPlatformClient",
)
