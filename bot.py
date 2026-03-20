"""
FluxerBot Core — Native Fluxer API Client
Hiçbir discord.py bağımlılığı yok.
Doğrudan api.fluxer.app ve gateway.fluxer.app ile konuşur.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger("FluxerBot")

API_BASE = "https://api.fluxer.app/v1"
GATEWAY_URL = "wss://gateway.fluxer.app/?v=1&encoding=json"

# Gateway Opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class FluxerHTTP:
    """Fluxer REST API istemcisi."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "FluxerBot/2.0",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def request(self, method: str, path: str, **kwargs) -> Any:
        session = await self._get_session()
        url = f"{API_BASE}{path}"
        kwargs.setdefault("headers", self.headers)
        async with session.request(method, url, **kwargs) as resp:
            if resp.status == 204:
                return None
            try:
                return await resp.json()
            except Exception:
                return await resp.text()

    async def send_message(self, channel_id: str, content: str = None, embed: dict = None, delete_after: float = None):
        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        result = await self.request("POST", f"/channels/{channel_id}/messages", json=payload)
        if delete_after and result and "id" in result:
            asyncio.create_task(self._delete_later(channel_id, result["id"], delete_after))
        return result

    async def _delete_later(self, channel_id: str, message_id: str, delay: float):
        await asyncio.sleep(delay)
        await self.delete_message(channel_id, message_id)

    async def delete_message(self, channel_id: str, message_id: str):
        try:
            await self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        except Exception:
            pass

    async def get_gateway(self) -> str:
        data = await self.request("GET", "/gateway/bot")
        if isinstance(data, dict) and "url" in data:
            return data["url"]
        return GATEWAY_URL

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class FluxerGateway:
    """Fluxer WebSocket Gateway bağlantısı."""

    def __init__(self, token: str, intents: int = 32767):
        self.token = token
        self.intents = intents
        self.http = FluxerHTTP(token)
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session_id: Optional[str] = None
        self._sequence: Optional[int] = None
        self._heartbeat_interval: float = 41.25
        self._last_heartbeat_ack: float = 0
        self._running = False
        self._handlers: Dict[str, List[Callable]] = {}
        self.user: Optional[Dict] = None

    def on(self, event: str):
        """Event handler dekoratörü."""
        def decorator(func: Callable):
            self._handlers.setdefault(event.upper(), []).append(func)
            return func
        return decorator

    def add_handler(self, event: str, func: Callable):
        self._handlers.setdefault(event.upper(), []).append(func)

    async def _send(self, data: dict):
        if self._ws and not self._ws.closed:
            await self._ws.send_str(json.dumps(data))

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self._heartbeat_interval / 1000)
            await self._send({"op": OP_HEARTBEAT, "d": self._sequence})

    async def _identify(self):
        await self._send({
            "op": OP_IDENTIFY,
            "d": {
                "token": self.token,
                "intents": self.intents,
                "properties": {
                    "$os": "linux",
                    "$browser": "FluxerBot",
                    "$device": "FluxerBot",
                },
                "presence": {
                    "status": "online",
                    "activities": [{
                        "name": "sunucuyu koruyorum 🛡️",
                        "type": 3
                    }]
                }
            },
        })

    async def _dispatch(self, event_name: str, data: Any):
        handlers = self._handlers.get(event_name.upper(), [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"Handler hatası [{event_name}]: {e}", exc_info=True)

    async def connect(self):
        self._running = True
        session = aiohttp.ClientSession()
        retry_delay = 5

        while self._running:
            try:
                gw_url = await self.http.get_gateway()
                if "?" not in gw_url:
                    gw_url += "?v=1&encoding=json"
                logger.info(f"Gateway'e bağlanılıyor: {gw_url}")

                async with session.ws_connect(gw_url, heartbeat=None) as ws:
                    self._ws = ws
                    retry_delay = 5
                    asyncio.create_task(self._heartbeat_loop())

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            await self._handle_payload(payload)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"WS bağlantısı kapandı: {msg}")
                            break

            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
                logger.warning(f"Gateway bağlantı hatası: {e} — {retry_delay}s sonra yeniden denenecek")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            except Exception as e:
                logger.error(f"Beklenmeyen gateway hatası: {e}", exc_info=True)
                await asyncio.sleep(retry_delay)

        await session.close()

    async def _handle_payload(self, payload: dict):
        op = payload.get("op")
        data = payload.get("d")
        seq = payload.get("s")
        event = payload.get("t")

        if seq is not None:
            self._sequence = seq

        if op == OP_HELLO:
            self._heartbeat_interval = data["heartbeat_interval"]
            logger.info(f"HELLO alındı — heartbeat: {self._heartbeat_interval}ms")
            await self._identify()

        elif op == OP_HEARTBEAT_ACK:
            self._last_heartbeat_ack = time.time()

        elif op == OP_HEARTBEAT:
            await self._send({"op": OP_HEARTBEAT, "d": self._sequence})

        elif op == OP_RECONNECT:
            logger.info("RECONNECT istendi")
            if self._ws:
                await self._ws.close()

        elif op == OP_INVALID_SESSION:
            logger.warning("Invalid session — yeniden identify ediliyor")
            await asyncio.sleep(5)
            self._session_id = None
            await self._identify()

        elif op == OP_DISPATCH and event:
            if event == "READY":
                self.user = data.get("user", {})
                self._session_id = data.get("session_id")
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                logger.info(f"  Bot hazır: {self.user.get('username')}#{self.user.get('discriminator', '0')}")
                logger.info(f"  Session: {self._session_id}")
                logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            await self._dispatch(event, data)

    async def close(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        await self.http.close()
