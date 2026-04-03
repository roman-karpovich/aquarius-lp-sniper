from loguru import logger
from stellar_sdk import SorobanServerAsync, xdr
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.keypair import Keypair
from stellar_sdk.soroban_rpc import RestorePreamble
from stellar_sdk.transaction_envelope import TransactionEnvelope

from tx_builder import aget_transaction_builder


async def restore_footprint_from_simulation(
    soroban_server: SorobanServerAsync,
    kp: Keypair,
    restore_preamble: RestorePreamble,
):
    te = (
        (await aget_transaction_builder(soroban_server, kp.public_key))
        .set_soroban_data(
            xdr.SorobanTransactionData.from_xdr(restore_preamble.transaction_data)
        )
        .append_restore_footprint_op()
        .build()
    )
    await submit_transaction(soroban_server, te, kp)


async def prepare_transaction(
    soroban_server: SorobanServerAsync,
    transaction: TransactionEnvelope,
    source_account: Keypair = None,
    signers: list[Keypair] = None,
    restore_account=None,
):
    simulation = await soroban_server.simulate_transaction(transaction)
    if simulation.latest_ledger and simulation.restore_preamble:
        logger.error(
            f"transaction failed to simulate. expired entry. xdr: {transaction.to_xdr()}"
        )
        await restore_footprint_from_simulation(
            soroban_server, restore_account or source_account, simulation.restore_preamble
        )
        return

    try:
        tx = await soroban_server.prepare_transaction(
            transaction, simulate_transaction_response=simulation
        )
    except PrepareTransactionException as ex:
        logger.error(f"transaction failed to prepare. xdr: {transaction.to_xdr()}")
        logger.error(
            f"prepare transaction error: {ex.simulate_transaction_response.error}"
        )
        return

    if source_account:
        tx.sign(source_account)
    for signer in signers or []:
        tx.sign(signer)

    return tx


async def submit_transaction(
    soroban_server: SorobanServerAsync,
    transaction: TransactionEnvelope,
    source_account: Keypair = None,
    signers: list[Keypair] = None,
    restore_account=None,
    include_fee: int = 0,
):
    simulation = await soroban_server.simulate_transaction(transaction)
    if simulation.latest_ledger and simulation.restore_preamble:
        logger.error(
            f"transaction failed to simulate. expired entry. xdr: {transaction.to_xdr()}"
        )
        await restore_footprint_from_simulation(
            soroban_server, restore_account or source_account, simulation.restore_preamble
        )
        return

    try:
        tx = await soroban_server.prepare_transaction(
            transaction, simulate_transaction_response=simulation
        )
    except PrepareTransactionException as ex:
        logger.error(f"transaction failed to prepare. xdr: {transaction.to_xdr()}")
        logger.error(
            f"prepare transaction error: {ex.simulate_transaction_response.error}"
        )
        return

    tx.transaction.fee += include_fee

    if source_account:
        tx.sign(source_account)
    for signer in signers or []:
        tx.sign(signer)

    await soroban_server.send_transaction(tx)
