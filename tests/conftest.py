"""Unit/regression tests never use real HomeTax sessions or external sockets."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("External network is disabled in the test suite; use a mock transport")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
