"""Process-lifetime provider pools, closed on application shutdown."""
import httpx

_clients = {}


def get_client(provider, timeout):
    client = _clients.get(provider)
    if client is None or client.is_closed:
        client = _clients[provider] = httpx.AsyncClient(timeout=timeout, follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
    return client


async def close_clients():
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.aclose()
