"""The Refresh now button, and the PSU headers it sends for a present user.

The headers are what lets a bank tell a user-initiated fetch from a
background one, and the 4/day PSD2 limit only applies to the latter. Sending
them when nobody is there would misrepresent a background poll, so most of
what is worth testing here is when they are *not* sent.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import current_request
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.enablebanking.api import EnableBankingClient
from custom_components.enablebanking.button import user_psu_headers
from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_PERSONAL,
)
from custom_components.enablebanking.models import AccountBalance

# A real public address: the documentation ranges (203.0.113.0/24 and
# friends) are not global, so the code rightly refuses to send them.
PUBLIC_IP = "8.8.8.8"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Test"
USER = Context(user_id="user-1")  # enough for the pure helper; presses use a real user


class _Request:
    """Just enough of an aiohttp request for `user_psu_headers`."""

    def __init__(self, remote: str | None, user_agent: str | None = USER_AGENT) -> None:
        self.remote = remote
        self.headers = CIMultiDict({"User-Agent": user_agent} if user_agent else {})


@pytest.fixture
def request_from() -> Generator[Any]:
    """Make `current_request` look like a frontend call from ``remote``."""

    def _set(remote: str | None, user_agent: str | None = USER_AGENT) -> None:
        current_request.set(_Request(remote, user_agent))

    yield _set
    # Not a token reset: async tests and their fixtures run in different
    # contexts, so a token from one cannot be reset in the other.
    current_request.set(None)


class TestUserPsuHeaders:
    """Who counts as a present user, and what gets sent for them."""

    def test_public_ip_sends_ip_and_user_agent(self, request_from: Any) -> None:
        request_from(PUBLIC_IP)

        assert user_psu_headers(USER) == {
            "Psu-Ip-Address": PUBLIC_IP,
            "Psu-User-Agent": USER_AGENT,
        }

    def test_ipv6_is_sent_too(self, request_from: Any) -> None:
        request_from("2001:4860:4860::8888")

        assert user_psu_headers(USER)["Psu-Ip-Address"] == "2001:4860:4860::8888"

    def test_missing_user_agent_still_sends_the_ip(self, request_from: Any) -> None:
        request_from(PUBLIC_IP, user_agent=None)

        assert user_psu_headers(USER) == {"Psu-Ip-Address": PUBLIC_IP}

    @pytest.mark.parametrize("remote", ["192.168.1.20", "10.0.0.5", "127.0.0.1", "::1"])
    def test_private_or_loopback_address_sends_nothing(
        self, request_from: Any, remote: str
    ) -> None:
        """Direct LAN access: an address like this means nothing to the bank."""
        request_from(remote)

        assert user_psu_headers(USER) == {}

    def test_press_without_a_user_sends_nothing(self, request_from: Any) -> None:
        """An automation's context has no user; that is a background poll."""
        request_from(PUBLIC_IP)

        assert user_psu_headers(Context()) == {}
        assert user_psu_headers(None) == {}

    def test_no_http_request_sends_nothing(self) -> None:
        assert user_psu_headers(USER) == {}

    def test_unparseable_address_sends_nothing(self, request_from: Any) -> None:
        request_from("not-an-ip")

        assert user_psu_headers(USER) == {}


class TestClientHeaders:
    def test_psu_headers_are_added_while_set(self) -> None:
        client = EnableBankingClient(MagicMock(), "a.b.c", "session-id")
        client.psu_headers = {"Psu-Ip-Address": PUBLIC_IP}

        assert client._headers["Psu-Ip-Address"] == PUBLIC_IP
        assert client._headers["Authorization"] == "Bearer a.b.c"

    def test_no_psu_headers_by_default(self) -> None:
        """Scheduled polls must go without them."""
        client = EnableBankingClient(MagicMock(), "a.b.c", "session-id")

        assert not any(name.startswith("Psu-") for name in client._headers)


@pytest.fixture
def account() -> AccountBalance:
    return AccountBalance(
        account_id="uid-one",
        stable_id="hash-one",
        iban="NL91ABNA0417164300",
        name="Betaalrekening",
        product="Current Account",
        currency="EUR",
        balance=1234.56,
        balance_type="CLBD",
        reference_date="2026-09-01",
        last_polled_at=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        title="ASN Bank",
        unique_id="abc123",
        data={
            CONF_JWT: "a.b.c",
            CONF_PRIVATE_KEY: "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
            CONF_APP_ID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            CONF_SESSION_ID: "11111111-2222-3333-4444-555555555555",
            CONF_ASPSP_NAME: "ASN Bank",
            CONF_ASPSP_COUNTRY: "NL",
            CONF_PSU_TYPE: PSU_PERSONAL,
        },
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


@pytest.fixture
def client(account: AccountBalance) -> MagicMock:
    """A client that records which PSU headers each balance fetch went out with."""
    mock = MagicMock()
    mock.psu_headers = {}
    mock.sent_headers = []

    async def _balances(*_args: Any, **_kwargs: Any) -> Any:
        mock.sent_headers.append(dict(mock.psu_headers))
        return {"hash-one": account}, set()

    mock.async_get_all_balances = AsyncMock(side_effect=_balances)
    return mock


def _button_entity_id(hass: HomeAssistant) -> str:
    registry = er.async_get(hass)
    entities = [
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.domain == "button"
    ]
    assert len(entities) == 1, f"expected exactly one refresh button, got {entities}"
    return entities[0]


@pytest.fixture
def user(hass_admin_user: MockUser) -> Context:
    """A press from a real logged-in user; HA checks it against the registry."""
    return Context(user_id=hass_admin_user.id)


async def _press(hass: HomeAssistant, context: Context) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _button_entity_id(hass)},
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()


class TestRefreshButton:
    async def test_press_by_a_present_user_polls_with_psu_headers(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        client: MagicMock,
        request_from: Any,
        user: Context,
    ) -> None:
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            request_from(PUBLIC_IP)
            await _press(hass, user)

        assert client.sent_headers == [{"Psu-Ip-Address": PUBLIC_IP, "Psu-User-Agent": USER_AGENT}]

    async def test_headers_are_cleared_after_the_press(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        client: MagicMock,
        request_from: Any,
        user: Context,
    ) -> None:
        """The next scheduled poll must not inherit them."""
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            request_from(PUBLIC_IP)
            await _press(hass, user)
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()

        assert client.sent_headers[-1] == {}
        assert client.psu_headers == {}

    async def test_press_from_an_automation_is_an_ordinary_poll(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        client: MagicMock,
        request_from: Any,
        user: Context,
    ) -> None:
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            request_from(PUBLIC_IP)
            await _press(hass, Context())

        assert client.sent_headers == [{}]

    async def test_press_over_the_lan_is_an_ordinary_poll(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        client: MagicMock,
        request_from: Any,
        user: Context,
    ) -> None:
        with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            request_from("192.168.1.20")
            await _press(hass, user)

        assert client.sent_headers == [{}]


async def test_headers_are_cleared_even_when_the_refresh_raises(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    with patch("custom_components.enablebanking.EnableBankingClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data

        with (
            patch.object(coordinator, "async_refresh", AsyncMock(side_effect=RuntimeError)),
            pytest.raises(RuntimeError),
        ):
            await coordinator.async_refresh_as_user({"Psu-Ip-Address": PUBLIC_IP})

    assert client.psu_headers == {}
