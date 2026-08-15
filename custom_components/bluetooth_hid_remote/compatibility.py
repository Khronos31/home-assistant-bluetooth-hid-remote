"""Explicit HAOS compatibility repair for malformed AR GATT discovery."""

from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.manager import BlueZManager, get_global_bluez_manager
from bleak.backends.bluezdbus.utils import assert_reply, get_dbus_authenticator
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.message import Message
from dbus_fast.signature import Variant
from homeassistant.core import HomeAssistant

from .const import HID_SERVICE_UUID
from .manager import bluez_device_path_from_address

_ADDRESS_RE = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")
_AR_NAME = "AR"
_AR_APPEARANCE = 0x0180
_BLUEZ_SERVICE = "org.bluez"
_DBUS_SERVICE = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"
_SYSTEMD_SERVICE = "org.freedesktop.systemd1"
_SYSTEMD_PATH = "/org/freedesktop/systemd1"
_SYSTEMD_MANAGER = "org.freedesktop.systemd1.Manager"
_SYSTEMD_PROPERTIES = "org.freedesktop.DBus.Properties"
_SYSTEMD_UNIT = "org.freedesktop.systemd1.Unit"
_SYSTEMD_SERVICE_UNIT = "org.freedesktop.systemd1.Service"
_HOST_CONFIG_ROOT = "/mnt/data/supervisor/homeassistant"
_BLUEZ_UNIT = "bluetooth.service"
_SERVICE_TIMEOUT = 20.0


class CompatibilityRepairError(Exception):
    """The host-side compatibility repair could not be completed."""


class CompatibilityRepairUnsupportedError(CompatibilityRepairError):
    """The selected device or host is not eligible for this repair."""


def _properties(manager: BlueZManager, path: str, interface: str) -> dict[str, Any]:
    try:
        return manager._properties[path][interface]
    except KeyError as err:
        raise CompatibilityRepairUnsupportedError(
            "BlueZ no longer exposes the selected device and adapter"
        ) from err


def is_ar_cache_repair_candidate(manager: BlueZManager, device_path: str) -> bool:
    """Match only the tested AR HOGP identity, not arbitrary HID devices."""
    properties = _properties(manager, device_path, defs.DEVICE_INTERFACE)
    uuids = {str(uuid).casefold() for uuid in properties.get("UUIDs", ())}
    return (
        properties.get("Name") == _AR_NAME
        and properties.get("Appearance") == _AR_APPEARANCE
        and HID_SERVICE_UUID in uuids
        and bool(properties.get("Paired"))
        and bool(properties.get("Bonded"))
    )


async def _async_open_system_bus() -> MessageBus:
    return await MessageBus(
        bus_type=BusType.SYSTEM,
        auth=get_dbus_authenticator(),
    ).connect()


async def _async_call(bus: MessageBus, message: Message):
    reply = await bus.call(message)
    assert_reply(reply)
    return reply


async def _async_systemd_unit(bus: MessageBus, member: str) -> None:
    await _async_call(
        bus,
        Message(
            destination=_SYSTEMD_SERVICE,
            path=_SYSTEMD_PATH,
            interface=_SYSTEMD_MANAGER,
            member=member,
            signature="ss",
            body=[_BLUEZ_UNIT, "replace"],
        ),
    )


async def _async_name_has_owner(bus: MessageBus, name: str) -> bool:
    reply = await _async_call(
        bus,
        Message(
            destination=_DBUS_SERVICE,
            path=_DBUS_PATH,
            interface=_DBUS_INTERFACE,
            member="NameHasOwner",
            signature="s",
            body=[name],
        ),
    )
    return bool(reply.body[0])


async def _async_wait_for_bluez(bus: MessageBus, *, running: bool) -> None:
    async with asyncio.timeout(_SERVICE_TIMEOUT):
        while await _async_name_has_owner(bus, _BLUEZ_SERVICE) is not running:
            await asyncio.sleep(0.1)


def _host_config_path(path: Path, config_dir: Path) -> str:
    try:
        # Local development may expose the integration through an absolute
        # /config/custom_components symlink. Resolve it before translating to
        # HAOS's host-side view so the transient helper never follows a host-
        # invalid /config target. A normal HACS installation resolves in place.
        relative = path.resolve().relative_to(config_dir.resolve())
    except ValueError as err:
        raise CompatibilityRepairUnsupportedError(
            "Compatibility assets are outside Home Assistant's config directory"
        ) from err
    return f"{_HOST_CONFIG_ROOT}/{relative.as_posix()}"


async def _async_install_cache(
    bus: MessageBus,
    *,
    helper_path: str,
    cache_path: str,
    adapter_address: str,
    device_address: str,
) -> None:
    unit = f"bluetooth-hid-cache-{secrets.token_hex(6)}.service"
    unit_path: str | None = None
    try:
        await _async_call(
            bus,
            Message(
                destination=_SYSTEMD_SERVICE,
                path=_SYSTEMD_PATH,
                interface=_SYSTEMD_MANAGER,
                member="StartTransientUnit",
                signature="ssa(sv)a(sa(sv))",
                body=[
                    unit,
                    "replace",
                    [
                        (
                            "Description",
                            Variant("s", "Install verified AR GATT cache"),
                        ),
                        ("Type", Variant("s", "oneshot")),
                        ("RemainAfterExit", Variant("b", True)),
                        ("TimeoutStartUSec", Variant("t", 10_000_000)),
                        (
                            "ExecStart",
                            Variant(
                                "a(sasb)",
                                [
                                    (
                                        "/bin/sh",
                                        [
                                            "/bin/sh",
                                            helper_path,
                                            cache_path,
                                            adapter_address,
                                            device_address,
                                        ],
                                        False,
                                    )
                                ],
                            ),
                        ),
                    ],
                    [],
                ],
            ),
        )
        unit_path = await _async_get_unit_path(bus, unit)
        await _async_wait_for_unit_success(bus, unit_path)
    finally:
        if unit_path is not None:
            with suppress(Exception):
                await _async_systemd_named_unit(bus, "StopUnit", unit)


async def _async_systemd_named_unit(bus: MessageBus, member: str, unit: str) -> None:
    await _async_call(
        bus,
        Message(
            destination=_SYSTEMD_SERVICE,
            path=_SYSTEMD_PATH,
            interface=_SYSTEMD_MANAGER,
            member=member,
            signature="ss",
            body=[unit, "replace"],
        ),
    )


async def _async_get_unit_path(bus: MessageBus, unit: str) -> str:
    reply = await _async_call(
        bus,
        Message(
            destination=_SYSTEMD_SERVICE,
            path=_SYSTEMD_PATH,
            interface=_SYSTEMD_MANAGER,
            member="GetUnit",
            signature="s",
            body=[unit],
        ),
    )
    return str(reply.body[0])


async def _async_get_unit_property(
    bus: MessageBus, unit_path: str, interface: str, name: str
) -> Any:
    reply = await _async_call(
        bus,
        Message(
            destination=_SYSTEMD_SERVICE,
            path=unit_path,
            interface=_SYSTEMD_PROPERTIES,
            member="Get",
            signature="ss",
            body=[interface, name],
        ),
    )
    return reply.body[0].value


async def _async_wait_for_unit_success(bus: MessageBus, unit_path: str) -> None:
    async with asyncio.timeout(_SERVICE_TIMEOUT):
        while True:
            state = await _async_get_unit_property(
                bus, unit_path, _SYSTEMD_UNIT, "ActiveState"
            )
            if state == "active":
                return
            if state == "failed":
                result = await _async_get_unit_property(
                    bus, unit_path, _SYSTEMD_SERVICE_UNIT, "Result"
                )
                raise CompatibilityRepairError(
                    f"The cache installer service failed: {result}"
                )
            await asyncio.sleep(0.1)


async def async_install_ar_gatt_cache(
    hass: HomeAssistant,
    address: str,
    *,
    manager: BlueZManager | None = None,
) -> None:
    """Install the tested non-secret AR cache after explicit user consent."""
    address = address.upper()
    if not _ADDRESS_RE.fullmatch(address):
        raise CompatibilityRepairUnsupportedError("Invalid Bluetooth address")
    if manager is None:
        manager = await get_global_bluez_manager()

    device_path = bluez_device_path_from_address(manager, address)
    if device_path is None or not is_ar_cache_repair_candidate(manager, device_path):
        raise CompatibilityRepairUnsupportedError(
            "The selected device does not match the tested AR compatibility profile"
        )

    device = _properties(manager, device_path, defs.DEVICE_INTERFACE)
    adapter_path = device.get("Adapter")
    if not isinstance(adapter_path, str):
        raise CompatibilityRepairUnsupportedError(
            "BlueZ did not report the direct adapter"
        )
    adapter = _properties(manager, adapter_path, defs.ADAPTER_INTERFACE)
    adapter_address = str(adapter.get("Address", "")).upper()
    if not _ADDRESS_RE.fullmatch(adapter_address):
        raise CompatibilityRepairUnsupportedError(
            "BlueZ reported an invalid adapter address"
        )

    config_dir = Path(hass.config.config_dir)
    component_dir = Path(__file__).parent
    helper_path = _host_config_path(
        component_dir / "compatibility" / "install-bluez-cache.sh", config_dir
    )
    cache_path = _host_config_path(
        component_dir / "compatibility" / "ar-gatt-cache", config_dir
    )

    try:
        bus = await _async_open_system_bus()
    except Exception as err:
        raise CompatibilityRepairUnsupportedError(
            "Could not open the HAOS system bus"
        ) from err

    bluez_stopped = False
    try:
        try:
            await _async_systemd_unit(bus, "StopUnit")
            bluez_stopped = True
            await _async_wait_for_bluez(bus, running=False)
            await _async_install_cache(
                bus,
                helper_path=helper_path,
                cache_path=cache_path,
                adapter_address=adapter_address,
                device_address=address,
            )
        except Exception as err:
            raise CompatibilityRepairError(
                "HAOS could not install the AR BlueZ GATT cache"
            ) from err
        finally:
            if bluez_stopped:
                try:
                    await _async_systemd_unit(bus, "StartUnit")
                    await _async_wait_for_bluez(bus, running=True)
                except Exception as err:
                    raise CompatibilityRepairError(
                        "The cache operation finished, but Bluetooth did not restart"
                    ) from err
    finally:
        bus.disconnect()
