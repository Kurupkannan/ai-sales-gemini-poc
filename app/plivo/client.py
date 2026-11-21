"""Small async Plivo client. Never automatically retry call creation."""
from uuid import UUID
import httpx
from app.config import settings
from app.http_clients import get_client
from app.timing import measure


class ProviderError(Exception):
    def __init__(self, *, uncertain: bool, status_code: int | None = None):
        super().__init__('Call provider request failed.')
        self.uncertain = uncertain
        self.status_code = status_code


def callback_url(kind: str, attempt_id) -> str:
    return f'{settings.public_origin}/plivo/{kind}/{attempt_id}'


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        with measure("plivo"):
            return await get_client("plivo", 15).request(method,
                f'https://api.plivo.com/v1/Account/{settings.PLIVO_AUTH_ID}/{path}',
                auth=(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN.get_secret_value()),
                **kwargs)
    except httpx.RequestError:
        raise ProviderError(uncertain=True) from None


async def create_call(attempt) -> str:
    response = await request('POST', 'Call/', json={
        'from': attempt.from_number, 'to': attempt.to_number,
        'answer_url': callback_url('answer', attempt.id), 'answer_method': 'POST',
        'ring_url': callback_url('ring', attempt.id), 'ring_method': 'POST',
        'hangup_url': callback_url('hangup', attempt.id), 'hangup_method': 'POST',
        'time_limit': settings.CALL_MAX_DURATION_SECONDS,
        'ring_timeout': settings.CALL_ANSWER_TIMEOUT_SECONDS,
    })
    if not 200 <= response.status_code < 300:
        raise ProviderError(uncertain=response.status_code not in (400, 401, 403, 404, 405, 422, 429), status_code=response.status_code)
    try:
        return str(UUID(response.json()['request_uuid']))
    except (ValueError, TypeError, KeyError):
        raise ProviderError(uncertain=True) from None


async def hangup(call_id: str) -> None:
    response = await request('DELETE', f'Call/{UUID(call_id)}/')
    if response.status_code not in (200, 202, 204, 404):
        raise ProviderError(uncertain=True, status_code=response.status_code)


async def cancel_request(request_id: str) -> None:
    response = await request('DELETE', f'Request/{UUID(request_id)}/')
    if response.status_code not in (200, 202, 204, 404):
        raise ProviderError(uncertain=True, status_code=response.status_code)


async def get_call(call_id: str) -> dict | None:
    response = await request('GET', f'Call/{UUID(call_id)}/')
    if response.status_code == 404:
        return None  # Missing CDR is not proof of no live call.
    if response.status_code != 200:
        raise ProviderError(uncertain=True, status_code=response.status_code)
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ValueError:
        raise ProviderError(uncertain=True) from None
