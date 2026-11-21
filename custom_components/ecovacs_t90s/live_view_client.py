"""Raw ECOVACS robot discovery used by the live view.

Derived from ``client.py`` of ECOVACS Live View
(https://github.com/imcdo17-cyber/home-assistant-ecovacs-live, v0.8.4, MIT
License, see NOTICE). Changes: the login part was removed; the live view now
uses the credentials of the integration's shared deebot-client
``Authenticator``. The account-credential reuse that was added locally to the
original ``client.py`` lives in ``controller.py`` now.

The live view needs some fields of the raw device record (``resource`` and the
``service.mqs`` host) exactly as the ECOVACS app receives them, so it asks for
the device list with the same parameters as the app.
"""

import base64
import json
import logging
from typing import Any

import aiohttp
from deebot_client.models import Credentials

_LOGGER = logging.getLogger(__name__)

_APP_ID = "ecovacs"
_APP_VERSION = "3.15.0"
_APP_CHANNEL = "google_play"
_REALM = "ecouser.net"


def _jwt_resource(token: str) -> str:
    """Extract the ECOVACS login resource from the JWT payload without verifying it.

    The token itself has already been returned by the authenticated ECOVACS login.
    ECOVACS HOME uses the JWT's ``r`` claim as auth.resource for
    GetGlobalDeviceList.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        if isinstance(data, dict):
            return str(data.get("r") or "")
    except Exception:
        _LOGGER.debug("Unable to decode ECOVACS JWT resource claim", exc_info=True)
    return ""


async def async_get_global_device_list(
    session: aiohttp.ClientSession,
    *,
    portal_url: str,
    credentials: Credentials,
    login_client_id: str,
) -> list[dict[str, Any]]:
    """Return the raw ECOVACS robot list using the app's proven API call.

    This reproduces ECOVACS HOME 3.15.0:
      POST /api/appsvr/app.do
      todo = GetGlobalDeviceList
    """
    user_id = str(credentials.user_id)
    token = str(credentials.token)
    auth_resource = (
        str(getattr(credentials, "resource", "") or "")
        or _jwt_resource(token)
        or login_client_id
    )

    auth = {
        "resource": auth_resource,
        "token": token,
        "realm": _REALM,
        "userid": user_id,
        "with": "users",
    }

    body = {
        "userid": user_id,
        "lang": "EN",
        "platform": "android",
        "appVer": _APP_VERSION,
        "channel": _APP_CHANNEL,
        "aliliving": True,
        "share": True,
        "software": "robotui_config",
        "auth": auth,
        "todo": "GetGlobalDeviceList",
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "appid": _APP_ID,
        "token": token,
        "userid": user_id,
        "Authorization": f"Bearer {token}",
    }

    url = f"{portal_url.rstrip('/')}/api/appsvr/app.do"

    async with session.post(
        url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
    ) as response:
        raw = await response.text()
        status = response.status

    if status >= 400:
        raise RuntimeError(f"ECOVACS GetGlobalDeviceList failed with HTTP {status}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ECOVACS GetGlobalDeviceList returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("ECOVACS GetGlobalDeviceList returned a non-object response")

    if str(payload.get("ret", "")).lower() != "ok":
        raise RuntimeError(
            "ECOVACS GetGlobalDeviceList failed "
            f"(code={payload.get('code')!r}, ret={payload.get('ret')!r}, "
            f"message={(payload.get('message') or payload.get('msg'))!r})"
        )

    raw_devices = payload.get("devices")
    if not isinstance(raw_devices, list):
        raise RuntimeError(
            "ECOVACS GetGlobalDeviceList response did not contain devices[]"
        )

    devices: list[dict[str, Any]] = []
    for value in raw_devices:
        if not isinstance(value, dict):
            continue
        device = dict(value)
        did = str(device.get("did", "")).strip()
        device_class = str(device.get("class", "")).strip()
        resource = str(device.get("resource", "")).strip()
        if not (did and device_class and resource):
            _LOGGER.warning(
                "Ignoring incomplete ECOVACS device record from GetGlobalDeviceList"
            )
            continue
        devices.append(device)

    _LOGGER.debug(
        "ECOVACS GetGlobalDeviceList returned %d usable robot(s)",
        len(devices),
    )
    return devices
