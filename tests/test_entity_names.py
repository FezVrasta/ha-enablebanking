"""Per-account entities carry their account, so siblings don't collide.

A bank is one device holding several accounts. Before this, every account's
spend sensors and transaction event were named only "Spent today" and
"Transaction", so the second account got `_2`, the third `_3`, and nothing in
the id or the name said which account was which.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enablebanking.const import (
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_FETCH_TRANSACTIONS,
    CONF_JWT,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    DOMAIN,
    PSU_PERSONAL,
)
from custom_components.enablebanking.entity import account_unique_id
from custom_components.enablebanking.models import AccountBalance

IBAN_ONE = "NL91ABNA0417164300"
IBAN_TWO = "NL02RABO0123456789"


def _account(stable_id: str, iban: str, name: str) -> AccountBalance:
    return AccountBalance(
        account_id=f"uid-{stable_id}",
        stable_id=stable_id,
        iban=iban,
        name=name,
        product="Current Account",
        currency="EUR",
        balance=100.0,
        balance_type="CLBD",
        reference_date="2026-09-01",
        last_polled_at=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
    )


@pytest.fixture
def accounts() -> dict[str, AccountBalance]:
    return {
        "hash-one": _account("hash-one", IBAN_ONE, "Betaalrekening"),
        "hash-two": _account("hash-two", IBAN_TWO, "Spaarrekening"),
    }


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
        options={CONF_FETCH_TRANSACTIONS: True},
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


def _client(accounts: dict[str, AccountBalance]) -> MagicMock:
    client = MagicMock()
    client.async_get_all_balances = AsyncMock(return_value=(accounts, set()))
    client.async_get_transactions = AsyncMock(return_value=[])
    return client


async def _setup_and_poll(
    hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
) -> None:
    with patch(
        "custom_components.enablebanking.EnableBankingClient", return_value=_client(accounts)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


def _entity_id(
    hass: HomeAssistant, entry: MockConfigEntry, domain: str, stable_id: str, key: str
) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, account_unique_id(entry.entry_id, stable_id, key)
    )
    assert entity_id is not None
    return entity_id


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    domain: str,
    stable_id: str,
    key: str,
    object_id: str,
) -> None:
    """Pretend an earlier version already created this entity under ``object_id``."""
    er.async_get(hass).async_get_or_create(
        domain,
        DOMAIN,
        account_unique_id(entry.entry_id, stable_id, key),
        suggested_object_id=object_id,
        config_entry=entry,
    )


class TestNewEntities:
    async def test_ids_carry_the_account(
        self, hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
    ) -> None:
        await _setup_and_poll(hass, entry, accounts)

        one, two = IBAN_ONE.lower(), IBAN_TWO.lower()
        assert _entity_id(hass, entry, "sensor", "hash-one", "spend_today") == (
            f"sensor.{one}_spent_today"
        )
        assert _entity_id(hass, entry, "sensor", "hash-two", "spend_30d") == (
            f"sensor.{two}_spent_last_30_days"
        )
        assert _entity_id(hass, entry, "event", "hash-two", "transaction") == (
            f"event.{two}_transaction"
        )

    async def test_names_carry_the_account(
        self, hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
    ) -> None:
        await _setup_and_poll(hass, entry, accounts)

        state = hass.states.get(_entity_id(hass, entry, "sensor", "hash-one", "spend_today"))
        assert state is not None
        assert state.attributes["friendly_name"] == f"ASN Bank {IBAN_ONE} spent today"

    async def test_account_without_iban_uses_its_name(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        await _setup_and_poll(hass, entry, {"hash-one": _account("hash-one", "", "Vault")})

        assert _entity_id(hass, entry, "sensor", "hash-one", "spend_today") == (
            "sensor.vault_spent_today"
        )


class TestLegacyIdMigration:
    async def test_auto_generated_ids_move_to_the_account(
        self, hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
    ) -> None:
        """What the second account was left with: `_2` on every entity."""
        _register(hass, entry, "sensor", "hash-one", "spend_today", "asn_bank_spent_today")
        _register(hass, entry, "sensor", "hash-two", "spend_today", "asn_bank_spent_today_2")
        _register(hass, entry, "event", "hash-two", "transaction", "asn_bank_transaction_2")

        await _setup_and_poll(hass, entry, accounts)

        one, two = IBAN_ONE.lower(), IBAN_TWO.lower()
        assert _entity_id(hass, entry, "sensor", "hash-one", "spend_today") == (
            f"sensor.{one}_spent_today"
        )
        assert _entity_id(hass, entry, "sensor", "hash-two", "spend_today") == (
            f"sensor.{two}_spent_today"
        )
        assert _entity_id(hass, entry, "event", "hash-two", "transaction") == (
            f"event.{two}_transaction"
        )

    async def test_a_users_own_rename_is_left_alone(
        self, hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
    ) -> None:
        _register(hass, entry, "sensor", "hash-one", "spend_today", "groceries_today")

        await _setup_and_poll(hass, entry, accounts)

        assert _entity_id(hass, entry, "sensor", "hash-one", "spend_today") == (
            "sensor.groceries_today"
        )

    async def test_a_taken_target_is_left_alone(
        self, hass: HomeAssistant, entry: MockConfigEntry, accounts: dict[str, AccountBalance]
    ) -> None:
        taken = f"{IBAN_ONE.lower()}_spent_today"
        er.async_get(hass).async_get_or_create(
            "sensor", "other_integration", "unrelated", suggested_object_id=taken
        )
        _register(hass, entry, "sensor", "hash-one", "spend_today", "asn_bank_spent_today")

        await _setup_and_poll(hass, entry, accounts)

        assert _entity_id(hass, entry, "sensor", "hash-one", "spend_today") == (
            "sensor.asn_bank_spent_today"
        )
