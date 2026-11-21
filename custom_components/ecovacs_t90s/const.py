"""Constants for the Ecovacs DEEBOT T90S integration.

Derived from the Home Assistant core ``ecovacs`` integration (Apache-2.0),
see NOTICE in the repository root.
"""

from enum import StrEnum

from deebot_client.commands import StationAction
from deebot_client.events import LifeSpan

DOMAIN = "ecovacs_t90s"

CONF_ACCOUNT_CREDENTIALS = "account_credentials"
CONF_OVERRIDE_REST_URL = "override_rest_url"
CONF_OVERRIDE_MQTT_URL = "override_mqtt_url"
CONF_VERIFICATION_CODE = "verification_code"
CONF_VERIFY_MQTT_CERTIFICATE = "verify_mqtt_certificate"

# Live view options (stored in the config entry options)
CONF_LIVE_VIEW_PIN = "live_view_pin"
CONF_AUTO_STOP_MINUTES = "auto_stop_minutes"
DEFAULT_AUTO_STOP_MINUTES = 10
MAX_AUTO_STOP_MINUTES = 120

SUPPORTED_LIFESPANS = (
    LifeSpan.AIR_FRESHENER,
    LifeSpan.BLADE,
    LifeSpan.BRUSH,
    LifeSpan.CLEANING_SOLUTION,
    LifeSpan.DUST_BAG,
    LifeSpan.DUST_BUCKET,
    LifeSpan.DUST_CASE_HEAP,
    LifeSpan.DUST_CONTAINER_FILTER,
    LifeSpan.FILTER,
    LifeSpan.HAND_FILTER,
    LifeSpan.HEAVY_DUTY_CLEANING_SOLUTION,
    LifeSpan.LENS_BRUSH,
    LifeSpan.MOP_WASHING_TRAY,
    LifeSpan.ROUND_MOP,
    LifeSpan.SEWAGE_BOX,
    LifeSpan.SIDE_BRUSH,
    LifeSpan.STATION_FILTER,
    LifeSpan.STRAINER,
    LifeSpan.TRIMMER_BRUSH,
    LifeSpan.UNIT_CARE,
    LifeSpan.UV_SANITIZER,
    LifeSpan.WATER_SINK,
    LifeSpan.WEED_ROPE,
)

SUPPORTED_STATION_ACTIONS = (
    StationAction.CLEAN_BASE,
    StationAction.DRY_MOP,
    StationAction.EMPTY_DUSTBIN,
    StationAction.WASH_MOP,
)


class InstanceMode(StrEnum):
    """Instance mode."""

    CLOUD = "cloud"
    SELF_HOSTED = "self_hosted"
