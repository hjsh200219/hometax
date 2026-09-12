from argparse import Namespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from test_invoice_operations import (
    ORIGINAL_A,
    FakeBackend,
    certificate_material,
    issue_request,
)

from hometax_login import cli
from hometax_login.errors import LoginError
from hometax_login.invoice_operations import InvoiceOperations
from hometax_login.protocol import HometaxClient
from hometax_login.write_journal import WriteJournal


def setup_cli(monkeypatch, tmp_path, *, yes=True):
    material = certificate_material()
    backend = FakeBackend()

    def no_network(request):
        pytest.fail("Tests must not call HomeTax")

    client = HometaxClient(transport=httpx.MockTransport(no_network))
    client.invoice_operations = InvoiceOperations(client, backend=backend)
    cert = tmp_path / "synthetic-cert"
    cert.write_bytes(b"fixture")
    entry = Namespace(cert_path=cert, key_path=cert)
    monkeypatch.setattr(cli, "pick_certificate", lambda args: entry)
    monkeypatch.setattr(cli, "read_password", lambda: "synthetic-password")
    monkeypatch.setattr(cli, "load_certificate", lambda *args: material)
    monkeypatch.setattr(cli, "build_client", lambda: client)
    monkeypatch.setattr(cli, "save_session", lambda *args: None)
    monkeypatch.setattr(cli, "open_journal", lambda: WriteJournal(tmp_path / "private" / "j.db"))
    monkeypatch.setattr(cli, "show_preview", lambda *args: None)
    monkeypatch.setattr(
        cli,
        "load_payload",
        lambda args: issue_request().model_dump(mode="json", exclude={"client_reference"}),
    )
    monkeypatch.setattr(cli, "session_client", AsyncMock(return_value=(client, {})))
    args = Namespace(
        yes=yes,
        wire="raw",
        login_type="04",
        approval=ORIGINAL_A,
        reason="duplicate_issue",
        written_date="2025-01-10",
    )
    return cli.Context(args), client, backend, material


@pytest.mark.parametrize("action", ["issue", "correct", "cancel"])
async def test_write_reauthenticates_selected_cert_instead_of_using_unbound_cache(
    monkeypatch, tmp_path, action
):
    context, client, backend, material = setup_cli(monkeypatch, tmp_path)
    assert client.signing_certificate_fingerprint is None
    if action == "cancel":
        monkeypatch.setattr(cli, "load_payload", lambda args: {})

    async def login(selected, login_type):
        assert selected is material
        assert not backend.preview_calls and not backend.issue_calls
        client.signing_certificate_fingerprint = material.certificate.fingerprint(
            hashes.SHA256()
        ).hex()
        return {"user_id": "synthetic-account"}

    client.login = AsyncMock(side_effect=login)
    assert await cli.invoice_operation(context, action) == 0
    client.login.assert_awaited_once_with(material, "04")
    cli.session_client.assert_not_awaited()
    assert len(backend.issue_calls) == 1
    assert client.http.is_closed


async def test_failed_reauthentication_never_previews_or_issues(monkeypatch, tmp_path):
    context, client, backend, material = setup_cli(monkeypatch, tmp_path)
    client.login = AsyncMock(side_effect=LoginError("LOGIN_REJECTED", "rejected", 401))
    with pytest.raises(LoginError):
        await cli.invoice_operation(context, "issue")
    assert not backend.preview_calls and not backend.issue_calls
    assert client.http.is_closed


async def test_same_cli_input_keeps_reference_without_writing(monkeypatch, tmp_path):
    context, client, backend, _ = setup_cli(monkeypatch, tmp_path, yes=False)
    references = []
    for _ in range(2):
        await cli.invoice_operation(context, "issue")
        references.append(backend.preview_calls[-1][1].client_reference)
    assert references[0] == references[1]
    assert not backend.issue_calls
    assert not (tmp_path / "private" / "j.db").exists()
