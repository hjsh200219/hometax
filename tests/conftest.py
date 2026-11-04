"""Unit/regression tests never use real HomeTax sessions or external sockets."""

import socket

import pytest

# Windows asyncio 는 이벤트 루프를 만들 때 socket.socketpair() 로 루프백 자기 연결을 맺는다
# (AF_UNIX socketpair 가 없어서). 루프백만 허용하고 나머지 연결은 계속 막는다.
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def is_loopback(address) -> bool:
        return isinstance(address, tuple) and address[0] in LOOPBACK_HOSTS

    def blocked(*args, **kwargs):
        pytest.fail("External network is disabled in the test suite; use a mock transport")

    def connect(self, address):
        return real_connect(self, address) if is_loopback(address) else blocked()

    def connect_ex(self, address):
        return real_connect_ex(self, address) if is_loopback(address) else blocked()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
