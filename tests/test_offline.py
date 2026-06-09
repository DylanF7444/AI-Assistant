import socket
import threading

import pytest

from assistant.policy.network import OfflineNetworkError, offline_lock


def test_offline_lock_blocks_outbound_connections():
    with offline_lock():
        with pytest.raises(OfflineNetworkError):
            socket.create_connection(("example.com", 80))


def test_offline_lock_allows_local_runtime_connections():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    address = server.getsockname()
    accepted = []

    def receive():
        connection, _ = server.accept()
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=receive)
    thread.start()
    with offline_lock():
        client = socket.create_connection(address)
        client.close()
    thread.join(timeout=2)
    server.close()
    assert accepted
