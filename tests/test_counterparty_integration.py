"""Real API -> real session client -> manager -> adapter -> mock HTTP; no live credentials."""

import copy
import hashlib
import json

import httpx
import pytest

from hometax_login.api import Settings, create_app
from hometax_login.protocol import HometaxClient

KEY = "integration-test-key-" * 3
NUMBER = "1234567890"


class Portal:
    def __init__(self):
        self.business = None
        self.contacts = []
        self.writes = []
        self.calls = []

    def __call__(self, request):
        action = request.url.params.get("actionId")
        self.calls.append(action or request.url.path)
        if request.url.path == "/token.do":
            return httpx.Response(200, json={"token": "synthetic-token"})
        if request.url.path == "/permission.do":
            return httpx.Response(
                200,
                json={
                    "resultMsg": {
                        "sessionMap": {
                            "userId": "synthetic-user",
                            "tin": "owner-tin",
                            "cnvrTin": "owner-tin",
                            "tnmNm": "예시공급사",
                            "userClsfCd": "02",
                            "txfrBmanLgnYn": "N",
                        }
                    }
                },
            )
        body = json.loads(request.content)
        out = {"resultMsg": {"result": "S"}}
        if action == "ATEETBAE001R06":
            rows = (
                []
                if self.business is None
                else [
                    {
                        **self.business,
                        "txprDscmNoEncCntnView": NUMBER,
                    }
                ]
            )
            out.update(
                myClplcListDVO=rows,
                pageInfoVO={
                    **body["pageInfoVO"],
                    "totalCount": len(rows),
                },
            )
        elif action == "ATTABZAA001R05":
            out["mpbCtlDVO"] = {"count": "0"}
        elif action == "ATTABZAA001R01":
            out["bmanCrpNtplDVO"] = {
                "tin": "recipient-tin",
                "txprClsfCd": "02",
                "txprDscmNoEncCntn": "synthetic-encrypted-number",
                "txprStatCd": "01",
            }
        elif action == "ATEETBAA001R04":
            out["response"] = {"clplcCnt": "0"}
        elif action == "ATEETBAE001R07":
            out["myClplcDtlDVO"] = copy.deepcopy(self.contacts)
        elif action in {"ATEETBAA001C01", "ATEETBAE001U02"}:
            self.writes.append(action)
            self.business = copy.deepcopy(body["tteetbd105DVO"])
            self.contacts = copy.deepcopy(body["tteetbd106DVO"])
            for index, row in enumerate(self.contacts, 1):
                row["chrgSn"] = row.get("chrgSn") or str(index)
        elif action == "ATEETBAE001D04":
            self.writes.append(action)
            self.business = None
            self.contacts = []
        else:
            pytest.fail(f"Unexpected action: {action}")
        return httpx.Response(200, json=out)


async def setup(tmp_path, *, enabled):
    portal = Portal()
    backend = HometaxClient(transport=httpx.MockTransport(portal))
    app = create_app(
        Settings(
            api_keys=(KEY,),
            counterparty_writes_enabled=enabled,
            write_journal_path=str(tmp_path / "private" / "writes.sqlite3"),
        )
    )
    session = await app.state.sessions.add(
        hashlib.sha256(KEY.encode()).hexdigest(),
        backend,
        {"user_id": "synthetic-user"},
    )
    return app, portal, session


@pytest.mark.asyncio
async def test_real_client_all_three_operations_and_idempotent_apply(tmp_path):
    app, portal, session = await setup(tmp_path, enabled=True)
    base = f"/v1/hometax/sessions/{session.id}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {KEY}"},
        ) as client:
            create = await client.post(
                base + "/counterparties",
                json={
                    "business_number": NUMBER,
                    "name": "예시거래처",
                    "business_item": "교육",
                    "primary_contact": {"email": "billing@example.test"},
                },
            )
            assert create.status_code == 200
            assert create.json()["requires_confirmation"] is True
            assert portal.writes == []
            create_id = create.json()["change_id"]
            first = await client.post(
                base + f"/counterparty-changes/{create_id}/apply", json={"confirm": True}
            )
            assert first.status_code == 200
            assert first.json()["status"] == "applied"
            again = await client.post(
                base + f"/counterparty-changes/{create_id}/apply", json={"confirm": True}
            )
            assert again.json()["status"] == "already_applied"
            update = await client.patch(
                base + f"/counterparties/{NUMBER}", json={"business_item": "컨설팅"}
            )
            assert update.status_code == 200
            assert update.json()["after"]["primary_contact"]["email"] == "billing@example.test"
            update_id = update.json()["change_id"]
            applied = await client.post(
                base + f"/counterparty-changes/{update_id}/apply", json={"confirm": True}
            )
            assert applied.status_code == 200
            assert portal.business["itmNm"] == "컨설팅"
            delete = await client.delete(base + f"/counterparties/{NUMBER}")
            assert delete.status_code == 200
            assert portal.business is not None
            delete_id = delete.json()["change_id"]
            applied = await client.post(
                base + f"/counterparty-changes/{delete_id}/apply", json={"confirm": True}
            )
            assert applied.status_code == 200
            assert portal.business is None
            assert portal.writes == ["ATEETBAA001C01", "ATEETBAE001U02", "ATEETBAE001D04"]
            assert (await client.delete(base)).status_code == 204
            assert not session.client.counterparty_changes.plans
    finally:
        await app.state.sessions.close()


@pytest.mark.asyncio
async def test_real_client_disabled_apply_does_not_call_manager_or_upstream(tmp_path):
    app, portal, session = await setup(tmp_path, enabled=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {KEY}"},
        ) as client:
            response = await client.post(
                f"/v1/hometax/sessions/{session.id}/counterparty-changes/{'x' * 32}/apply",
                json={"confirm": True},
            )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "COUNTERPARTY_WRITES_DISABLED"
        assert response.headers["cache-control"] == "no-store"
        assert portal.calls == []
    finally:
        await app.state.sessions.close()
