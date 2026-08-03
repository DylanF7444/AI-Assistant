"""Extension discovery and installation services."""

from assistant.extensions.platform import (
    CatalogExtension,
    ExtensionCatalog,
    ExtensionHubService,
    ExtensionPlatformClient,
    ManagedExtensionRegistry,
    ManagedExtensionSpec,
)

__all__ = (
    "CatalogExtension",
    "ExtensionCatalog",
    "ExtensionHubService",
    "ExtensionPlatformClient",
    "ManagedExtensionRegistry",
    "ManagedExtensionSpec",
)
