"""Bounded Graph API requests. Never return/log provider bodies or credential URLs."""
import hashlib
import hmac
import logging
import re
import time
import httpx
from app.config import settings
from app.http_clients import get_client
from app.timing import measure


class _NoGraphURLs(logging.Filter):
    def filter(self, record):
        return "graph.facebook.com" not in record.getMessage()


logging.getLogger("httpx").addFilter(_NoGraphURLs())
logging.getLogger("httpcore").setLevel(logging.WARNING)


class MetaError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def asset_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value):
        raise MetaError("Meta returned an invalid business asset ID.")
    return value


class MetaClient:
    def __init__(self):
        self.base = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

    async def call(self, method, path, *, token=None, params=None, data=None, payload=None):
        query = dict(params or {})
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            query["appsecret_proof"] = hmac.new(settings.META_APP_SECRET.get_secret_value().encode(), token.encode(), hashlib.sha256).hexdigest()
        try:
            with measure("meta"):
                response = await get_client("meta", 20).request(method, f"{self.base}/{path}", headers=headers,
                                                params=query, data=data, json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise MetaError("Meta did not return a usable response. Retry setup; if code exchange failed, start signup again.") from None
        if not isinstance(body, dict):
            raise MetaError("Meta returned an unexpected response. Retry setup.")
        if response.is_error or "error" in body:
            error = body.get("error", {})
            code = error.get("code") if isinstance(error, dict) else None
            if code == 190:
                raise MetaError("Meta authorization expired or was revoked. Disconnect and reconnect WhatsApp.")
            if code in (10, 200):
                raise MetaError("Meta denied access. Check app permissions, test roles, and business asset access.")
            raise MetaError(f"Meta request failed (HTTP {response.status_code}). Check the account setup in Meta and retry.")
        return body

    async def exchange(self, code):
        data = await self.call("POST", "oauth/access_token", data={"client_id": settings.META_APP_ID,
                               "client_secret": settings.META_APP_SECRET.get_secret_value(), "code": code})
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise MetaError("Meta did not return an access token. Start signup again.")
        return token

    async def inspect(self, token):
        app_token = f"{settings.META_APP_ID}|{settings.META_APP_SECRET.get_secret_value()}"
        result = await self.call("GET", "debug_token", token=app_token, params={"input_token": token})
        data = result.get("data", {})
        if not isinstance(data, dict) or not data.get("is_valid") or str(data.get("app_id")) != settings.META_APP_ID:
            raise MetaError("Token is invalid or belongs to a different Meta app. Reconnect WhatsApp.")
        scopes = data.get("scopes", [])
        if not {"whatsapp_business_management", "whatsapp_business_messaging"}.issubset(set(scopes)):
            raise MetaError("Both WhatsApp management and messaging permissions are required. Reconnect with the correct configuration.")
        expiries = [int(data[k]) for k in ("expires_at", "data_access_expires_at") if data.get(k)]
        expiry = min(expiries) if expiries else None
        if expiry and expiry <= time.time():
            raise MetaError("Meta authorization has expired. Reconnect WhatsApp.")
        wabas = set()
        for scope in data.get("granular_scopes", []):
            if scope.get("scope") == "whatsapp_business_management":
                wabas.update(asset_id(x) for x in scope.get("target_ids", []))
        return {"scopes": scopes, "expires_at": expiry, "wabas": sorted(wabas)}

    async def phones(self, token, waba):
        waba = asset_id(waba)
        # Direct API authorization, not browser session event IDs, is authoritative.
        await self.call("GET", waba, token=token, params={"fields": "id,name"})
        items, after = [], None
        for _ in range(20):
            query = {"fields": "id,display_phone_number,verified_name,code_verification_status,platform_type", "limit": 100}
            if after: query["after"] = after
            result = await self.call("GET", f"{waba}/phone_numbers", token=token, params=query)
            for phone in result.get("data", []):
                items.append({"id": asset_id(phone["id"]), "display_phone_number": phone.get("display_phone_number", ""),
                              "verified_name": phone.get("verified_name", ""), "code_verification_status": phone.get("code_verification_status"),
                              "platform_type": phone.get("platform_type")})
            paging = result.get("paging", {})
            if not paging.get("next"): return items
            after = paging.get("cursors", {}).get("after")
            if not after: break
        raise MetaError("The account contains too many numbers for this pilot. Use a smaller test account.")

    async def subscribe(self, token, waba):
        path = f"{asset_id(waba)}/subscribed_apps"
        result = await self.call("POST", path, token=token)
        if result.get("success") is not True:
            raise MetaError("Meta did not confirm the webhook subscription. Retry setup.")
        result = await self.call("GET", path, token=token)
        if not any(str(item.get("whatsapp_business_api_data", {}).get("id")) == settings.META_APP_ID for item in result.get("data", [])):
            raise MetaError("Webhook subscription is not yet visible for this app. Retry setup.")

    async def register(self, token, phone, pin):
        result = await self.call("POST", f"{asset_id(phone)}/register", token=token,
                                 payload={"messaging_product": "whatsapp", "pin": pin})
        if result.get("success") is not True:
            raise MetaError("Meta did not confirm number registration. Check WhatsApp Manager before retrying.")
