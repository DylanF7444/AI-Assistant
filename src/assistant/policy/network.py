from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator


class OfflineNetworkError(PermissionError):
    pass


@contextlib.contextmanager
def offline_lock(enabled: bool = True) -> Iterator[None]:
    if not enabled:
        yield
        return
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def is_loopback(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        if str(host).casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def blocked_connect(sock, address):
        if is_loopback(address):
            return original_connect(sock, address)
        raise OfflineNetworkError("Outbound network access is blocked by offline mode")

    def blocked_create_connection(address, *args, **kwargs):
        if is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        raise OfflineNetworkError("Outbound network access is blocked by offline mode")

    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection
