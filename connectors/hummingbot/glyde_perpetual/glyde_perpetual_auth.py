# Copyright 2026 Glyde. Apache-2.0; adapted from Hummingbot hyperliquid_perpetual.
"""Private reads and signed exchange actions use independent credentials."""

import json

from hummingbot.core.web_assistant.auth import AuthBase

from .glyde_perpetual_wire import Wire


class GlydePerpetualAuth(AuthBase):
    def __init__(self, settings):
        self.wire = Wire(settings)

    async def rest_authenticate(self, request):
        settings = self.wire.settings
        if request.url == settings.url("/info"):
            body = json.loads(request.data) if isinstance(request.data, str) else request.data
            body, headers = self.wire.info(body)
            request.headers = {**(request.headers or {}), **headers}
            request.data = json.dumps(body)
        elif request.url == settings.url("/exchange"):
            body = json.loads(request.data) if isinstance(request.data, str) else request.data
            request.data = self.wire.sign(body)
        else:
            raise ValueError("refusing credentials for a different endpoint")
        return request

    async def ws_authenticate(self, request):
        # The WS handshake carries the read credential; trading signatures never authorize reads.
        return request
