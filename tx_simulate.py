from typing import Iterable

from requests import RequestException
from stellar_sdk import SorobanServer, SorobanServerAsync, TransactionEnvelope
from stellar_sdk.xdr import SCVal


def simulate_transaction_results(
    soroban_server: SorobanServer,
    transaction: TransactionEnvelope,
) -> SCVal | Iterable[SCVal]:
    simulation = soroban_server.simulate_transaction(transaction)

    if simulation.error:
        raise RequestException(None, simulation.error)

    results = [SCVal.from_xdr(r.xdr) for r in simulation.results]
    if len(results) == 1:
        return results[0]
    return results


async def asimulate_transaction_results(
    soroban_server: SorobanServerAsync,
    transaction: TransactionEnvelope,
) -> SCVal | Iterable[SCVal]:
    simulation = await soroban_server.simulate_transaction(transaction)

    if simulation.error:
        raise RequestException(None, simulation.error)

    results = [SCVal.from_xdr(r.xdr) for r in simulation.results]
    if len(results) == 1:
        return results[0]
    return results
