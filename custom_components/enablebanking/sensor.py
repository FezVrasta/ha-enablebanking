"""Sensor platform for the Enable Banking integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util.dt import utcnow

from .const import CONF_ASPSP_NAME, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntity, account_unique_id
from .models import AccountBalance

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EnableBankingSensorDescription(SensorEntityDescription):
    """Describes an Enable Banking sensor entity."""

    value_fn: Callable[[AccountBalance], StateType] = lambda _: None
    account_attrs_fn: Callable[[AccountBalance], dict[str, Any]] | None = None


BALANCE_SENSOR = EnableBankingSensorDescription(
    key="balance",
    translation_key="balance",
    native_unit_of_measurement=CURRENCY_EURO,
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=2,
    icon="mdi:bank",
    value_fn=lambda acc: round(acc.balance, 2),
    account_attrs_fn=lambda acc: {
        "iban": acc.iban,
        "account_name": acc.name,
        "product": acc.product,
        "currency": acc.currency,
        "balance_type": acc.balance_type,
        "reference_date": acc.reference_date,
    },
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enable Banking balance sensors.

    Accounts discovered on a later poll are added without a reload. Accounts
    present only in the on-disk cache are picked up at boot via
    ``coordinator._cached`` (which has already seeded ``coordinator.data`` in
    ``async_load_cache`` by the time we reach here).
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_for_new_accounts() -> None:
        # Re-point any pre-0.6.5 uid-based entities to the stable-id scheme
        # BEFORE adding, so the added entity reuses the existing registry entry
        # (and its entity_id / history) instead of orphaning it.
        _migrate_unique_ids(hass, entry, coordinator)

        new_entities: list[EnableBankingBalanceSensor] = []
        seen_ids: set[str] = set()
        if coordinator.data is not None:
            seen_ids.update(coordinator.data.accounts.keys())
        # Also surface sensors for accounts that exist only in the cache
        # (e.g. first boot after an HA restart, before the first post-boot
        # poll has run). Keys are stable_ids.
        seen_ids.update(
            stable_id for stable_id in coordinator._cached
        )  # noqa: SLF001 — intentional direct access

        for stable_id in seen_ids:
            if stable_id in known:
                continue
            known.add(stable_id)
            new_entities.append(
                EnableBankingBalanceSensor(coordinator, BALANCE_SENSOR, stable_id)
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_for_new_accounts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_for_new_accounts))


@callback
def _migrate_unique_ids(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    coordinator: EnableBankingCoordinator,
) -> None:
    """Migrate old ``{entry}_{uid}_balance`` unique_ids to the stable-id scheme.

    Enable Banking rotates the account ``uid`` on every reauth, so uid-based
    unique_ids used to spawn a fresh entity each time. We now key on
    ``stable_id`` (identification_hash). This re-points the *currently active*
    entity for each account in place — preserving its entity_id and history —
    using the live uid↔stable_id mapping from the latest poll. Historical
    orphans from earlier reauths can't be mapped and are left for the user to
    delete. Idempotent: after migration the old unique_id no longer exists.
    """
    data = coordinator.data
    if data is None:
        return
    registry = er.async_get(hass)
    for ab in data.accounts.values():
        uid = ab.account_id
        if not uid:
            continue
        old_unique = f"{entry.entry_id}_{uid}_{BALANCE_SENSOR.key}"
        new_unique = account_unique_id(entry.entry_id, ab.stable_id, BALANCE_SENSOR.key)
        if old_unique == new_unique:
            continue
        old_eid = registry.async_get_entity_id("sensor", DOMAIN, old_unique)
        new_eid = registry.async_get_entity_id("sensor", DOMAIN, new_unique)
        if old_eid and not new_eid:
            registry.async_update_entity(old_eid, new_unique_id=new_unique)
            _LOGGER.info(
                "Enable Banking: migrated %s to a session-stable unique_id",
                old_eid,
            )


class EnableBankingBalanceSensor(EnableBankingEntity, SensorEntity):
    """Balance sensor for one Enable Banking account.

    Always returns the last known balance — fresh from the coordinator if
    the latest poll had it, otherwise from the persistent cache. The sensor
    never goes ``unavailable`` or returns ``unknown`` as long as at least
    one successful poll has ever happened for this account.
    """

    entity_description: EnableBankingSensorDescription
    coordinator: EnableBankingCoordinator

    @property
    def name(self) -> str | None:
        account = self._current_account
        if account is None:
            return "Balance"
        if account.iban:
            return f"Balance {account.iban}"
        if account.name:
            return f"Balance {account.name}"
        return "Balance"

    @property
    def _current_account(self) -> AccountBalance | None:
        """Best-effort account lookup by stable_id: fresh data, else cache."""
        data = self.coordinator.data
        if data is not None and self._stable_id in data.accounts:
            return data.accounts[self._stable_id]
        return self.coordinator.cached_account(self._stable_id)

    @property
    def available(self) -> bool:
        # Intentionally does NOT chain to super().available. The base
        # CoordinatorEntity ties availability to the last poll's success,
        # which would flip the sensor to unavailable on a transient network
        # blip or a rate-limit response. We want exactly the opposite:
        # show the last known value with a `stale` attribute if need be.
        return self._current_account is not None

    @property
    def native_value(self) -> StateType:
        account = self._current_account
        if account is None:
            return None
        return self.entity_description.value_fn(account)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        account = self._current_account
        if account is None or self.entity_description.account_attrs_fn is None:
            return None

        attrs = self.entity_description.account_attrs_fn(account)
        attrs["aspsp"] = self.coordinator.config_entry.data.get(CONF_ASPSP_NAME)
        attrs["last_polled_at"] = (
            account.last_polled_at.isoformat() if account.last_polled_at else None
        )
        attrs["last_error"] = self.coordinator.last_error
        attrs["stale"] = _is_stale(account, self.coordinator.update_interval)

        data = self.coordinator.data
        if data is not None and data.consent_expires_at is not None:
            attrs["consent_expires_at"] = data.consent_expires_at.isoformat()
            attrs["consent_days_remaining"] = max(
                0, (data.consent_expires_at - utcnow()).days
            )

        return attrs


def _is_stale(
    account: AccountBalance, update_interval: timedelta | None
) -> bool:
    """True if the last successful poll for this account is older than 2× interval."""
    if account.last_polled_at is None:
        return True
    interval = update_interval or timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    return (utcnow() - account.last_polled_at) > 2 * interval
