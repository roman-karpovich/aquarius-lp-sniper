from stellar_sdk import Server, ServerAsync, SorobanServer, SorobanServerAsync
from stellar_sdk.account import Account

_CACHE: dict[str, Account] = {}


def clear_cache() -> None:
    _CACHE.clear()


def get_account_cached(server: Server | SorobanServer, public_key: str) -> Account:
    if public_key not in _CACHE:
        _CACHE[public_key] = server.load_account(public_key)
    return _CACHE[public_key]


async def aget_account_cached(
    server: ServerAsync | SorobanServerAsync, public_key: str
) -> Account:
    if public_key not in _CACHE:
        _CACHE[public_key] = await server.load_account(public_key)
    return _CACHE[public_key]
