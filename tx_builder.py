from stellar_sdk import (
    Network,
    Server,
    ServerAsync,
    SorobanServer,
    SorobanServerAsync,
    TransactionBuilder,
)


def get_transaction_builder(
    server: SorobanServer | Server, public_key: str, base_fee=None
) -> TransactionBuilder:
    account = server.load_account(public_key)
    return TransactionBuilder(
        account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=base_fee or 1000000,
    ).set_timeout(20)


async def aget_transaction_builder(
    server: SorobanServerAsync | ServerAsync,
    public_key: str,
    base_fee=None,
    timeout: int = 20,
) -> TransactionBuilder:
    account = await server.load_account(public_key)
    return TransactionBuilder(
        account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=base_fee or 1000000,
    ).set_timeout(timeout)
