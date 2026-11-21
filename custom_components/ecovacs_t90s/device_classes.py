"""Support for robot models that deebot-client does not know (yet).

deebot-client looks up the capabilities of a robot by its device class (the
``class`` field of the Ecovacs device list). Robots with an unknown class end
up in ``Devices.not_supported`` and get no entities. Some unknown classes are
variants of a supported model with the same firmware and command set, so we
reuse the capabilities of that supported model.

deebot-client 18.7.0 has no public API to register an extra class:
``deebot_client.hardware.get_static_device_info`` imports the module
``deebot_client.hardware.<class>`` and caches the result in the private
``_DEVICES`` dict, and caches misses in the private ``_NOT_FOUND`` set. We put
the base model's ``StaticDeviceInfo`` into that cache. Every access to the
private attributes is guarded; if they change in a future deebot-client
release, a warning is logged and the robot is reported as not supported
instead of breaking setup.

The mapping is skipped for a class that deebot-client supports natively, so it
becomes a no-op once deebot-client adds the class. To support another model,
add an entry to ``EXTRA_DEVICE_CLASSES``.

Note: deebot-client is shared by everything in the Home Assistant process, so
the mapping also becomes visible to the built-in ``ecovacs`` integration if
that one discovers devices after this integration did.
"""

from collections.abc import Mapping
import logging
from typing import Final

from deebot_client import hardware

_LOGGER = logging.getLogger(__name__)

# Unknown device class -> supported device class whose capabilities are reused.
EXTRA_DEVICE_CLASSES: Final[Mapping[str, str]] = {
    # DEEBOT T90S PRO OMNI (Care Kit) -> DEEBOT T90 PRO OMNI
    "833gj5": "twunby",
}

STATUS_NATIVE: Final = "native"
STATUS_MAPPED: Final = "mapped"
STATUS_FAILED: Final = "failed"

# Classes this module added to the deebot-client cache in this process.
_MAPPED: dict[str, str] = {}


async def async_register_extra_device_classes(
    mapping: Mapping[str, str] = EXTRA_DEVICE_CLASSES,
) -> dict[str, str]:
    """Make deebot-client recognise the classes in ``mapping``.

    Must run before device discovery (``ApiClient.get_devices``). It is
    idempotent and cheap after the first call.
    Returns a status per class: native, mapped or failed.
    """
    result: dict[str, str] = {}
    for device_class, base_class in mapping.items():
        result[device_class] = await _async_register(device_class, base_class)
    return result


async def _async_register(device_class: str, base_class: str) -> str:
    """Register one extra device class and return its status."""
    if await hardware.get_static_device_info(device_class) is not None:
        if device_class in _MAPPED:
            # Registered by an earlier setup in this process
            return STATUS_MAPPED
        _LOGGER.debug(
            "deebot-client supports device class %s natively, no mapping needed",
            device_class,
        )
        return STATUS_NATIVE

    base_info = await hardware.get_static_device_info(base_class)
    if base_info is None:
        _LOGGER.warning(
            "Cannot add support for Ecovacs device class %s: deebot-client does "
            "not know its base class %s",
            device_class,
            base_class,
        )
        return STATUS_FAILED

    devices = getattr(hardware, "_DEVICES", None)
    not_found = getattr(hardware, "_NOT_FOUND", None)
    if not isinstance(devices, dict) or not isinstance(not_found, set):
        _LOGGER.warning(
            "Cannot add support for Ecovacs device class %s: the internals of "
            "deebot_client.hardware changed (expected the dict _DEVICES and the "
            "set _NOT_FOUND). This integration needs an update; the robot is "
            "reported as not supported until then",
            device_class,
        )
        return STATUS_FAILED

    devices[device_class] = base_info
    not_found.discard(device_class)

    # Verify that the public lookup now returns the base model's info.
    if await hardware.get_static_device_info(device_class) is not base_info:
        devices.pop(device_class, None)
        _LOGGER.warning(
            "Cannot add support for Ecovacs device class %s: deebot-client "
            "ignored the registered capabilities. This integration needs an "
            "update; the robot is reported as not supported until then",
            device_class,
        )
        return STATUS_FAILED

    _MAPPED[device_class] = base_class
    _LOGGER.info(
        "Using the deebot-client capabilities of device class %s for the "
        "unsupported device class %s",
        base_class,
        device_class,
    )
    return STATUS_MAPPED
