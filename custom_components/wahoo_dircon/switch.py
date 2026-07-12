from homeassistant.components import switch
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import BaseEntity
from .constants import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_setup_entities):
    coordinator = hass.data[DOMAIN]["devices"][entry.entry_id]
    async_setup_entities([_Enabled(coordinator), _AutoConnect(coordinator)])

class _Enabled(BaseEntity, switch.SwitchEntity):

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.with_name("Connect")
        self._attr_device_class = "switch"
        self._attr_icon = "mdi:power"

    def on_data_update(self, data: dict):
        self._attr_is_on = self.coordinator.enabled

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_toggle_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_toggle_enabled(False)

class _AutoConnect(BaseEntity, switch.SwitchEntity, RestoreEntity):

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.with_name("Auto Connect")
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:lan-connect"
        self._attr_is_on = True

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"
        self.coordinator.set_auto_connect(self._attr_is_on)

    async def async_turn_on(self, **kwargs):
        self._attr_is_on = True
        self.coordinator.set_auto_connect(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._attr_is_on = False
        self.coordinator.set_auto_connect(False)
        self.async_write_ha_state()
