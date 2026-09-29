"""Base entity for the Enable Banking integration."""

from __future__ import annotations

import hashlib
import logging
import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_ASPSP_COUNTRY, CONF_ASPSP_NAME, CONF_PSU_TYPE, DOMAIN
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator

_LOGGER = logging.getLogger(__name__)


def account_unique_id(entry_id: str, stable_id: str, key: str) -> str:
    """Build a stable per-account entity unique_id.

    ``stable_id`` (Enable Banking's ``identification_hash``) can contain ``/``,
    ``+`` and ``=``; hash it to a compact hex token so the unique_id is clean
    and stays identical across sessions. Both entity creation and the one-time
    migration in ``sensor.py`` must use this helper so their ids agree.
    """
    token = hashlib.sha256(stable_id.encode()).hexdigest()[:16]
    return f"{entry_id}_{token}_{key}"


class EnableBankingEntity(CoordinatorEntity[EnableBankingCoordinator]):
    """Base entity for Enable Banking sensors."""

    _attr_has_entity_name = True
    _attr_attribution = "Data via Enable Banking AIS (PSD2)"

    def __init__(
        self,
        coordinator: EnableBankingCoordinator,
        description: EntityDescription,
        stable_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._stable_id = stable_id
        self._attr_unique_id = account_unique_id(
            coordinator.config_entry.entry_id, stable_id, description.key
        )

        entry = coordinator.config_entry
        aspsp_name = entry.data.get(CONF_ASPSP_NAME, "Enable Banking")
        country = entry.data.get(CONF_ASPSP_COUNTRY, "")
        psu_type = entry.data.get(CONF_PSU_TYPE, "")
        model_parts = [p for p in (country, psu_type) if p]

        # Put the bank in `manufacturer` so the service-info card reads
        # "<country · psu_type> / door <Bank>", which is the info users
        # actually care about on a balance card. The "data via Enable
        # Banking" provenance stays visible through the attribution on
        # each entity and the integration card title.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=aspsp_name,
            manufacturer=aspsp_name,
            model=" · ".join(model_parts) if model_parts else "Account",
            entry_type=DeviceEntryType.SERVICE,
        )


def account_entity_id(
    domain: str, coordinator: EnableBankingCoordinator, stable_id: str, suffix: str
) -> str:
    """The entity_id a new per-account entity asks for: ``<domain>.<account>_<suffix>``.

    Without the bank in front, like the balance sensor's ``sensor.<iban>``. The
    friendly name keeps the bank, since it comes from the device.
    """
    return f"{domain}.{slugify(account_label(coordinator, stable_id))}_{suffix}"


def account_label(coordinator: EnableBankingCoordinator, stable_id: str) -> str:
    """How an account is told apart in its entities' names.

    The IBAN, like the balance sensor's name, else the account name, else a
    short token. A bank holds several accounts under one device, so a per-account
    entity named only "Spent today" collides with its siblings as `_2`, `_3`.
    """
    account = None
    if coordinator.data is not None:
        account = coordinator.data.accounts.get(stable_id)
    if account is None:
        account = coordinator.cached_account(stable_id)
    if account is not None:
        if account.iban:
            return account.iban
        if account.name:
            return account.name
    return stable_id[:8]


@callback
def async_rename_legacy_entity_ids(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    coordinator: EnableBankingCoordinator,
    domain: str,
    object_ids: dict[str, str],
) -> None:
    """Move per-account entities off the ids they got before names had the account.

    ``object_ids`` maps a description key to the object id suffix its entities
    use, e.g. ``spend_today`` to ``spent_today``. From the bare name, entities
    used to get ``<bank>_spent_today``, and the second account
    ``<bank>_spent_today_2``; they now get ``<account>_spent_today``, matching
    the balance sensor's ``sensor.<iban>``.

    Guards, as for the balance sensor's IBAN rename: only an id still in that
    auto-generated shape is touched, so a user's own rename is never
    overwritten, and only when the new id is free.
    """
    registry = er.async_get(hass)
    device = slugify(entry.data.get(CONF_ASPSP_NAME, "Enable Banking"))
    stable_ids = set(coordinator.cached_stable_ids())
    if coordinator.data is not None:
        stable_ids.update(coordinator.data.accounts)

    for stable_id in stable_ids:
        label = slugify(account_label(coordinator, stable_id))
        for key, suffix in object_ids.items():
            unique_id = account_unique_id(entry.entry_id, stable_id, key)
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id is None:
                continue
            if not re.fullmatch(rf"{domain}\.{device}_{suffix}(_\d+)?", entity_id):
                continue
            target = f"{domain}.{label}_{suffix}"
            if registry.async_get(target) is not None:
                _LOGGER.debug("Enable Banking: cannot rename %s to %s (taken)", entity_id, target)
                continue
            registry.async_update_entity(entity_id, new_entity_id=target)
            _LOGGER.info("Enable Banking: renamed %s to %s", entity_id, target)
