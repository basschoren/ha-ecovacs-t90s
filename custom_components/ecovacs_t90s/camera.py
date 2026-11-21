"""Camera platform for the robot live view.

Derived from ``camera.py`` of ECOVACS Live View
(https://github.com/imcdo17-cyber/home-assistant-ecovacs-live, v0.8.4, MIT
License, see NOTICE). Changes: attaches to the robot device of this
integration and uses a translated entity name; the streaming code is unchanged.
"""

import asyncio
import base64
import time
from typing import override

from aiohttp import web
from deebot_client.device import Device

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import EcovacsLiveViewEntity
from .live_view_runtime import LiveViewRuntime

# Small valid JPEG used only to keep an idle MJPEG connection alive when no
# previous robot frame exists.  Keeping the HTTP stream open prevents clients
# from entering a rapid connect/EOF/reconnect loop while Live View is idle.
_IDLE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAJABADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJgAD//Z"
)

_IDLE_KEEPALIVE_SECONDS = 10.0
_IDLE_CHECK_SECONDS = 0.5
_LIVE_CHECK_SECONDS = 0.05


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the live view camera entities."""
    controller = config_entry.runtime_data
    if (live_view := controller.live_view) is None:
        return
    async_add_entities(
        EcovacsLiveCamera(device, live_view) for device in controller.devices
    )


class EcovacsLiveCamera(EcovacsLiveViewEntity, Camera):
    """Live camera backed by the robot Kinesis/WebRTC video track."""

    _attr_content_type = "image/jpeg"

    def __init__(self, device: Device, live_view: LiveViewRuntime) -> None:
        """Initialize the camera."""
        Camera.__init__(self)
        EcovacsLiveViewEntity.__init__(self, device, live_view, "live_view")

    @property
    @override
    def is_streaming(self) -> bool:
        """Return whether the ECOVACS WebRTC session is active."""
        return self._live_view.manager.is_streaming(self.did)

    @override
    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes:
        """Return a valid JPEG without starting Live View.

        Home Assistant exposes this method through /api/camera_proxy and uses
        that URL as the camera entity picture.  Returning None while idle makes
        the proxy request fail, which can cause frontends to retry the thumbnail
        repeatedly.  Return the latest robot frame when available, otherwise a
        tiny static JPEG.
        """
        return self._live_view.manager.get_latest_image(self.did) or _IDLE_JPEG

    @staticmethod
    async def _write_mjpeg_frame(
        response: web.StreamResponse,
        jpeg: bytes,
    ) -> None:
        """Write one complete MJPEG part."""
        header = (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
        )
        await response.write(header)
        await response.write(jpeg)
        await response.write(b"\r\n")

    @override
    async def handle_async_mjpeg_stream(
        self,
        request: web.Request,
    ) -> web.StreamResponse:
        """Serve robot frames while keeping the MJPEG connection stable when idle.

        Ending the HTTP response immediately whenever the robot's WebRTC session
        is idle makes camera clients reconnect in a loop. This handler keeps
        each client connection open. While idle it sleeps cheaply and sends only
        an occasional cached/placeholder JPEG to keep intermediaries from timing
        the stream out. Once Live View starts, new robot frames are forwarded
        without requiring the client to reconnect.
        """
        manager = self._live_view.manager
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        last_sequence = -1
        last_keepalive = 0.0

        try:
            while True:
                transport = request.transport
                if transport is None or transport.is_closing():
                    break

                if manager.is_streaming(self.did):
                    sequence = manager.get_image_sequence(self.did)
                    if sequence != last_sequence:
                        jpeg = manager.get_latest_image(self.did)
                        if jpeg:
                            await self._write_mjpeg_frame(response, jpeg)
                            last_sequence = sequence
                            last_keepalive = time.monotonic()
                    await asyncio.sleep(_LIVE_CHECK_SECONDS)
                    continue

                # Idle: do not terminate the response.  Ending it immediately can
                # make dashboards reconnect continuously.  Send a very infrequent
                # frame only to keep the established HTTP stream healthy.
                now = time.monotonic()
                if now - last_keepalive >= _IDLE_KEEPALIVE_SECONDS:
                    jpeg = manager.get_latest_image(self.did) or _IDLE_JPEG
                    await self._write_mjpeg_frame(response, jpeg)
                    last_keepalive = now

                await asyncio.sleep(_IDLE_CHECK_SECONDS)

        except ConnectionResetError, BrokenPipeError, asyncio.CancelledError:
            pass
        except RuntimeError:
            # aiohttp may raise RuntimeError when a client closes during a write.
            pass
        finally:
            try:
                await response.write_eof()
            except ConnectionResetError, BrokenPipeError, RuntimeError:
                pass

        return response
