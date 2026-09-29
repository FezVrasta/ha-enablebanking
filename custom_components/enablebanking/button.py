"""Refresh button for the Enable Banking integration."""

from __future__ import annotations

import logging
from ipaddress import ip_address

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.http import current_request

from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import EnableBankingEntity

_LOGGER = logging.getLogger(__name__)

REFRESH_BUTTON = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    icon="mdi:refresh",
)


def user_psu_headers(context: Context | None) -> dict[str, str]:
    """PSU headers for the user behind this press, or {} if there is none.

    Only a press made by a logged-in user from the frontend qualifies:
    automations have no user_id, and a private or loopback address (direct
    LAN access) means nothing to the bank.
    """
    if context is None or context.user_id is None:
        return {}
    request = current_request.get()
    if request is None or not request.remote:
        return {}
    try:
        address = ip_address(request.remote)
    except ValueError:
        return {}
    if not address.is_global:
        return {}
    headers = {"Psu-Ip-Address": str(address)}
    if user_agent := request.headers.get("User-Agent"):
        headers["Psu-User-Agent"] = user_agent
    return headers


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the refresh button for one bank connection."""
    async_add_entities([EnableBankingRefreshButton(entry.runtime_data)])


class EnableBankingRefreshButton(EnableBankingEntity, ButtonEntity):
    """Poll the bank now, as the user who pressed the button."""

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator, REFRESH_BUTTON, coordinator.config_entry.entry_id)

    async def async_press(self) -> None:
        psu_headers = user_psu_headers(self._context)
        request = current_request.get()
        _LOGGER.debug(
            "Refresh pressed for entry %s (user %s, remote %s); PSU headers: %s",
            self.coordinator.config_entry.entry_id,
            self._context.user_id if self._context else None,
            request.remote if request else None,
            psu_headers or "not sent",
        )
        await self.coordinator.async_refresh_as_user(psu_headers)
