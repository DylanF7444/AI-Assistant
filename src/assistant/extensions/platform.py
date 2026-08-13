from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assistant import API_VERSION
from assistant.contracts import PermissionScope, RiskLevel, SkillManifest
from assistant.integrations.connections import (
    ConnectionBinding,
    ConnectionState,
    ConnectionStatus,
)
from assistant.skills.base import Skill
from assistant.skills.registry import ExtensionExecutionClass, SkillRegistry
from assistant.storage.database import AuditStore

CATALOG_SCHEMA_VERSION = "1.0"
DEFAULT_CATALOG_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_PACKAGE_MAX_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 512


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_https_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Extension platform URLs must use HTTPS without embedded credentials")
    return value


class CatalogExtension(_StrictModel):
    extension_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    extension_type: str = "skill"
    version: str
    api_version: str
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    publisher: str = Field(min_length=1, max_length=120)
    category: str = Field(default="General", min_length=1, max_length=80)
    package_url: str
    package_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    package_format: Literal["zip"] = "zip"
    permissions: tuple[PermissionScope, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    featured: bool = False
    source: Literal["platform", "first_party"] = "platform"

    @field_validator("package_url")
    @classmethod
    def _package_url_must_be_https(cls, value: str) -> str:
        return _validate_https_url(value)

    @property
    def compatible(self) -> bool:
        return self.extension_type == "skill" and self.api_version == API_VERSION


class ExtensionCatalog(_StrictModel):
    schema_version: Literal["1.0"] = CATALOG_SCHEMA_VERSION
    extensions: tuple[CatalogExtension, ...] = ()

    @model_validator(mode="after")
    def _extension_ids_are_unique(self) -> ExtensionCatalog:
        ids = [item.extension_id for item in self.extensions]
        if len(ids) != len(set(ids)):
            raise ValueError("Extension catalog contains duplicate extension IDs")
        return self


@dataclass(frozen=True)
class ManagedExtensionSpec:
    """One reviewed implementation and all of its Hub composition metadata."""

    extension: CatalogExtension
    manifest: SkillManifest
    factory: Callable[[], Skill]
    connection: ConnectionBinding | None = None

    def __post_init__(self) -> None:
        if self.extension.source != "first_party":
            raise ValueError("Managed extensions must use the first_party source")
        implementation = self.factory()
        if not isinstance(implementation, Skill):
            raise TypeError("Managed extension factory must return a Skill")
        if implementation.manifest != self.manifest:
            raise ValueError("Managed extension manifest does not match its factory")
        _verify_catalog_metadata(self.extension, self.manifest)

    @property
    def extension_id(self) -> str:
        return self.extension.extension_id

    @classmethod
    def from_factory(
        cls,
        factory: Callable[[], Skill],
        *,
        publisher: str = "Local Assistant",
        package_slug: str | None = None,
        featured: bool = False,
        connection: ConnectionBinding | None = None,
    ) -> ManagedExtensionSpec:
        implementation = factory()
        if not isinstance(implementation, Skill):
            raise TypeError("Managed extension factory must return a Skill")
        manifest = SkillManifest.model_validate(implementation.manifest)
        slug = package_slug or manifest.skill_id.replace(".", "-").replace("_", "-")
        if not slug or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in slug
        ):
            raise ValueError("Managed extension package slug is invalid")
        extension = CatalogExtension(
            extension_id=manifest.skill_id,
            extension_type="skill",
            version=manifest.version,
            api_version=manifest.api_version,
            display_name=manifest.display_name,
            description=manifest.description,
            publisher=publisher,
            category=manifest.category,
            package_url=f"https://extensions.localassistant.invalid/{slug}",
            package_sha256=hashlib.sha256(manifest.model_dump_json().encode("utf-8")).hexdigest(),
            permissions=manifest.permissions,
            risk=manifest.risk,
            featured=featured,
            source="first_party",
        )
        return cls(
            extension=extension,
            manifest=manifest,
            factory=factory,
            connection=connection,
        )


class ManagedExtensionRegistry:
    """Provider-neutral source of managed factories, catalog entries and connections."""

    def __init__(self, specs: tuple[ManagedExtensionSpec, ...] = ()) -> None:
        ids = [spec.extension_id for spec in specs]
        if len(ids) != len(set(ids)):
            raise ValueError("Managed extension registry contains duplicate extension IDs")
        self._specs = {spec.extension_id: spec for spec in specs}

    def __contains__(self, extension_id: str) -> bool:
        return extension_id in self._specs

    def get(self, extension_id: str) -> ManagedExtensionSpec:
        try:
            return self._specs[extension_id]
        except KeyError as error:
            raise KeyError(f"Unknown managed extension: {extension_id}") from error

    @property
    def specs(self) -> tuple[ManagedExtensionSpec, ...]:
        return tuple(self._specs.values())

    @property
    def catalog_extensions(self) -> tuple[CatalogExtension, ...]:
        return tuple(spec.extension for spec in self._specs.values())

    @property
    def skill_factories(self) -> dict[str, Callable[[], Skill]]:
        return {extension_id: spec.factory for extension_id, spec in self._specs.items()}

    def connection(self, extension_id: str) -> ConnectionBinding | None:
        spec = self._specs.get(extension_id)
        return spec.connection if spec else None


Reader = Callable[[str, int, float], bytes]


def _validate_public_https(url: str) -> None:
    _validate_https_url(url)
    parsed = urlparse(url)
    assert parsed.hostname is not None
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as error:
        raise ValueError(f"Could not resolve extension platform host: {parsed.hostname}") from error
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError("Private, local, and reserved extension platform targets are blocked")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_public_https(url: str, max_bytes: int, timeout_seconds: float) -> bytes:
    _validate_public_https(url)
    request = Request(
        url,
        headers={
            "Accept": "application/json, application/zip",
            "User-Agent": "LocalAssistant/0.1 ExtensionHub",
        },
    )
    try:
        with build_opener(_SafeRedirectHandler()).open(
            request, timeout=timeout_seconds
        ) as response:
            payload = response.read(max_bytes + 1)
    except HTTPError as error:
        raise RuntimeError(f"Extension platform returned HTTP {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Extension platform request failed: {error}") from error
    if len(payload) > max_bytes:
        raise ValueError(f"Extension platform response exceeds the {max_bytes} byte limit")
    return payload


class ExtensionPlatformClient:
    def __init__(
        self,
        catalog_url: str | None,
        *,
        timeout_seconds: float = 15,
        package_max_bytes: int = DEFAULT_PACKAGE_MAX_BYTES,
        reader: Reader | None = None,
    ) -> None:
        self.catalog_url = _validate_https_url(catalog_url) if catalog_url else None
        self.timeout_seconds = timeout_seconds
        self.package_max_bytes = package_max_bytes
        self._reader = reader or _read_public_https

    def configure(self, catalog_url: str | None, *, timeout_seconds: float | None = None) -> None:
        self.catalog_url = _validate_https_url(catalog_url) if catalog_url else None
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds

    def fetch_catalog(self) -> ExtensionCatalog:
        if not self.catalog_url:
            raise RuntimeError("No extension platform URL is configured")
        raw = self._reader(
            self.catalog_url,
            DEFAULT_CATALOG_MAX_BYTES,
            self.timeout_seconds,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Extension platform returned invalid catalog JSON") from error
        return ExtensionCatalog.model_validate(payload)

    def download(self, extension: CatalogExtension) -> bytes:
        payload = self._reader(
            extension.package_url,
            self.package_max_bytes,
            self.timeout_seconds,
        )
        digest = hashlib.sha256(payload).hexdigest()
        if digest.casefold() != extension.package_sha256.casefold():
            raise ValueError("Downloaded extension package digest does not match the catalog")
        return payload


class ExtensionHubService:
    def __init__(
        self,
        registry: SkillRegistry,
        client: ExtensionPlatformClient,
        *,
        audit: AuditStore | None = None,
        managed_registry: ManagedExtensionRegistry | None = None,
        managed_extensions: tuple[tuple[CatalogExtension, SkillManifest], ...] = (),
        connection_services: dict[str, Any] | None = None,
        connection_dialogs: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.client = client
        self.audit = audit
        if managed_registry is not None and (
            managed_extensions or connection_services or connection_dialogs
        ):
            raise ValueError(
                "Use managed_registry or the legacy managed extension arguments, not both"
            )
        self.managed_registry = managed_registry or self._legacy_managed_registry(
            managed_extensions,
            connection_services or {},
            connection_dialogs or {},
        )
        self._catalog = ExtensionCatalog(extensions=self.managed_registry.catalog_extensions)

    @property
    def catalog_url(self) -> str | None:
        return self.client.catalog_url

    @property
    def catalog(self) -> ExtensionCatalog:
        return self._catalog

    def configure(self, catalog_url: str | None, *, timeout_seconds: float | None = None) -> None:
        previous_url = self.client.catalog_url
        self.client.configure(catalog_url, timeout_seconds=timeout_seconds)
        if previous_url != self.client.catalog_url:
            self._catalog = ExtensionCatalog(extensions=self.managed_registry.catalog_extensions)

    async def refresh(self) -> tuple[CatalogExtension, ...]:
        remote = await asyncio.to_thread(self.client.fetch_catalog)
        managed = self.managed_registry.catalog_extensions
        catalog = ExtensionCatalog(
            extensions=(
                *managed,
                *(
                    extension
                    for extension in remote.extensions
                    if extension.extension_id not in self.managed_registry
                ),
            )
        )
        self._catalog = catalog
        self._record(
            event_type="extension_catalog_refreshed",
            decision="allowed",
            details={"catalog_url": self.catalog_url, "count": len(catalog.extensions)},
        )
        return catalog.extensions

    def is_installed(self, extension_id: str) -> bool:
        install_dir = self.registry.install_dir
        return bool(install_dir and (install_dir / extension_id / "manifest.json").is_file())

    def installation_status(
        self, extension_id: str
    ) -> Literal["available", "installed", "id_in_use"]:
        if self.is_installed(extension_id):
            return "installed"
        try:
            self.registry.inspect(extension_id)
        except KeyError:
            return "available"
        return "id_in_use"

    def execution_class(self, extension_id: str) -> ExtensionExecutionClass:
        if extension_id in self.managed_registry:
            return ExtensionExecutionClass.MANAGED_IN_PROCESS
        return (
            ExtensionExecutionClass.REVIEWED_WORKER
            if self.registry.allow_reviewed_worker_extensions
            else ExtensionExecutionClass.BLOCKED_NATIVE
        )

    def is_installable(self, extension: CatalogExtension) -> bool:
        return extension.compatible and self.execution_class(extension.extension_id) != (
            ExtensionExecutionClass.BLOCKED_NATIVE
        )

    def connection_service(self, extension_id: str) -> Any | None:
        binding = self.managed_registry.connection(extension_id)
        return binding.service if binding else None

    def connection_dialog_service(self, extension_id: str) -> Any | None:
        binding = self.managed_registry.connection(extension_id)
        return binding.dialog_service() if binding else None

    def connection_binding(self, extension_id: str) -> ConnectionBinding | None:
        return self.managed_registry.connection(extension_id)

    def connection_dialog_entry_point(self, extension_id: str) -> str | None:
        binding = self.connection_binding(extension_id)
        return binding.dialog_entry_point if binding else None

    def connection_status(self, extension_id: str) -> ConnectionStatus | None:
        binding = self.connection_binding(extension_id)
        return binding.status() if binding else None

    def skill_availability(
        self,
        extension_id: str,
        *,
        connection_required: bool = True,
    ) -> Literal[
        "enabled",
        "disabled",
        "disconnected",
        "reauthorization_required",
        "unavailable",
    ]:
        availability = self.registry.availability(extension_id)
        status = self.connection_status(extension_id)
        if connection_required and availability != "unavailable" and status is not None:
            if status.state in {
                ConnectionState.MISSING_SCOPES,
                ConnectionState.REAUTHORIZATION_REQUIRED,
                ConnectionState.ERROR,
            }:
                return "reauthorization_required"
            if not status.usable:
                return "disconnected"
        return availability

    def sync_connection_state(self, extension_id: str) -> bool:
        """Enable an installed connection skill while its account remains connected."""
        binding = self.connection_binding(extension_id)
        if binding is None:
            return False
        try:
            self.registry.inspect(extension_id)
        except KeyError:
            return False
        status = binding.status()
        if status.usable or not binding.connection_required:
            self.registry.enable(extension_id)
        else:
            self.registry.disable(extension_id)
        return status.usable

    def sync_connected_extensions(self) -> tuple[str, ...]:
        return tuple(
            spec.extension_id
            for spec in self.managed_registry.specs
            if spec.connection is not None
            if self.sync_connection_state(spec.extension_id)
        )

    async def install(self, extension_id: str) -> SkillManifest:
        extension = next(
            (item for item in self._catalog.extensions if item.extension_id == extension_id),
            None,
        )
        if extension is None:
            raise KeyError("Refresh the catalog before installing this extension")
        if not extension.compatible:
            raise ValueError(
                f"Extension type {extension.extension_type!r} or API {extension.api_version!r} "
                "is not supported by this build"
            )
        status = self.installation_status(extension.extension_id)
        if status == "installed":
            raise FileExistsError(f"{extension.display_name} is already installed")
        if status == "id_in_use":
            raise ValueError("This extension ID is already used by a built-in skill")
        try:
            if not self.is_installable(extension):
                raise PermissionError(
                    "Downloaded Python extensions are blocked because this build has no "
                    "OS-enforced hostile-code sandbox."
                )
            if extension_id in self.managed_registry:
                manifest = await asyncio.to_thread(self._install_managed, extension)
            else:
                manifest = await asyncio.to_thread(self._install_skill, extension)
        except Exception as error:
            self._record(
                event_type="extension_installation",
                skill_id=extension.extension_id,
                decision="failed",
                permissions=[scope.capability for scope in extension.permissions],
                details={"version": extension.version, "error": str(error)},
            )
            raise
        self._record(
            event_type="extension_installation",
            skill_id=manifest.skill_id,
            decision="installed_disabled",
            permissions=[scope.capability for scope in manifest.permissions],
            details={"version": manifest.version, "publisher": extension.publisher},
        )
        return manifest

    async def uninstall(self, extension_id: str) -> None:
        """Remove an extension installed through the hub.

        The catalog entry is used for audit context, but an installed extension
        remains removable if a later catalog refresh no longer lists it.
        Built-in skills are protected by the same ID-in-use check used during
        installation.
        """

        extension = next(
            (item for item in self._catalog.extensions if item.extension_id == extension_id),
            None,
        )
        permissions = [scope.capability for scope in extension.permissions] if extension else []
        details: dict[str, Any] = {}
        if extension is not None:
            details.update(version=extension.version, publisher=extension.publisher)

        try:
            status = self.installation_status(extension_id)
            if status == "id_in_use":
                raise ValueError("This extension ID is used by a built-in skill")
            if status != "installed":
                raise FileNotFoundError(f"{extension_id} is not installed")
            await asyncio.to_thread(self.registry.uninstall, extension_id)
            install_dir = self.registry.install_dir
            if install_dir is not None and (
                (install_dir / extension_id).exists() or (install_dir / extension_id).is_symlink()
            ):
                raise OSError(f"Could not fully remove the installed extension: {extension_id}")
        except Exception as error:
            self._record(
                event_type="extension_uninstallation",
                skill_id=extension_id,
                decision="failed",
                permissions=permissions,
                details={**details, "error": str(error)},
            )
            raise

        self._record(
            event_type="extension_uninstallation",
            skill_id=extension_id,
            decision="uninstalled",
            permissions=permissions,
            details=details,
        )

    def _install_managed(self, extension: CatalogExtension) -> SkillManifest:
        spec = self.managed_registry.get(extension.extension_id)
        _verify_catalog_metadata(extension, spec.manifest)
        installed = self.registry.install_managed(spec.manifest)
        try:
            self.registry.load_installed(installed.skill_id)
        except Exception:
            self.registry.uninstall(installed.skill_id)
            raise
        return installed

    def _legacy_managed_registry(
        self,
        managed_extensions: tuple[tuple[CatalogExtension, SkillManifest], ...],
        connection_services: dict[str, Any],
        connection_dialogs: dict[str, str],
    ) -> ManagedExtensionRegistry:
        specs: list[ManagedExtensionSpec] = []
        managed_ids = {extension.extension_id for extension, _manifest in managed_extensions}
        unknown_services = set(connection_services).difference(managed_ids)
        unknown_dialogs = set(connection_dialogs).difference(managed_ids)
        if unknown_services or unknown_dialogs:
            raise ValueError("Connection metadata references an unknown managed extension")
        for extension, manifest in managed_extensions:
            factory = self.registry.managed_skill_factories.get(extension.extension_id)
            if factory is None:
                raise ValueError(
                    f"Managed extension factory is unavailable: {extension.extension_id}"
                )
            service = connection_services.get(extension.extension_id)
            binding = (
                ConnectionBinding(
                    service=service,
                    provider_id=getattr(service, "provider_id", extension.extension_id),
                    dialog_entry_point=connection_dialogs.get(extension.extension_id),
                )
                if service is not None
                else None
            )
            specs.append(
                ManagedExtensionSpec(
                    extension=extension,
                    manifest=manifest,
                    factory=factory,
                    connection=binding,
                )
            )
        return ManagedExtensionRegistry(tuple(specs))

    def _install_skill(self, extension: CatalogExtension) -> SkillManifest:
        payload = self.client.download(extension)
        install_dir = self.registry.install_dir
        if install_dir is None:
            raise RuntimeError("No extension installation directory is configured")
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="extension-stage-", dir=install_dir) as temp:
            staging = Path(temp)
            package_dir = _extract_package(payload, staging)
            manifest = self.registry.validate_package(package_dir)
            _verify_catalog_metadata(extension, manifest)
            installed = self.registry.install(package_dir)
            try:
                self.registry.load_installed(installed.skill_id)
            except Exception:
                self.registry.uninstall(installed.skill_id)
                raise
        return installed

    def _record(
        self,
        *,
        event_type: str,
        decision: str,
        skill_id: str | None = None,
        permissions: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            correlation_id=str(uuid4()),
            event_type=event_type,
            skill_id=skill_id,
            decision=decision,
            permissions=permissions,
            details=details,
        )


def _extract_package(payload: bytes, destination: Path) -> Path:
    archive_path = destination / "extension.zip"
    archive_path.write_bytes(payload)
    extraction_root = destination / "package"
    extraction_root.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("Extension archive has an invalid number of files")
            expanded_limit = max(DEFAULT_PACKAGE_MAX_BYTES, len(payload) * 8)
            if sum(member.file_size for member in members) > expanded_limit:
                raise ValueError("Extension archive expands beyond the safety limit")
            for member in members:
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                if (
                    not relative.parts
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or relative.parts[0].endswith(":")
                ):
                    raise ValueError("Extension archive contains an unsafe path")
                file_mode = member.external_attr >> 16
                if stat.S_ISLNK(file_mode):
                    raise ValueError("Extension archive cannot contain symbolic links")
                target = extraction_root.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
    except zipfile.BadZipFile as error:
        raise ValueError("Extension package is not a valid ZIP archive") from error

    if (extraction_root / "manifest.json").is_file():
        return extraction_root
    roots = [path for path in extraction_root.iterdir() if path.is_dir()]
    files = [path for path in extraction_root.iterdir() if path.is_file()]
    if len(roots) == 1 and not files and (roots[0] / "manifest.json").is_file():
        return roots[0]
    raise ValueError("Extension archive must contain one package with manifest.json at its root")


def _verify_catalog_metadata(
    extension: CatalogExtension,
    manifest: SkillManifest,
) -> None:
    expected = {
        "skill_id": extension.extension_id,
        "version": extension.version,
        "api_version": extension.api_version,
        "display_name": extension.display_name,
        "description": extension.description,
        "category": extension.category,
        "permissions": extension.permissions,
        "risk": extension.risk,
    }
    mismatches = [name for name, value in expected.items() if getattr(manifest, name) != value]
    if mismatches:
        raise ValueError(
            "Extension package metadata does not match the catalog: " + ", ".join(mismatches)
        )
