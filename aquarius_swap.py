"""
Token acquisition via Aquarius AMM.

Uses Aquarius find-path API to get swap quotes, then executes swaps
through the Aquarius router's swap_chained function.
"""

import aiohttp
from loguru import logger
from stellar_sdk import Keypair, SorobanServerAsync, scval
from stellar_sdk.xdr import SCVal

import settings
from tx_builder import aget_transaction_builder
from tx_simulate import asimulate_transaction_results
from tx_submit import submit_transaction


async def get_balance(
    server: SorobanServerAsync,
    public_key: str,
    token_address: str,
) -> int:
    """Get token balance for an address via simulation."""
    builder = await aget_transaction_builder(server, public_key)
    tx = (
        builder.append_invoke_contract_function_op(
            contract_id=token_address,
            function_name="balance",
            parameters=[scval.to_address(public_key)],
        )
        .build()
    )
    try:
        result = await asimulate_transaction_results(server, tx)
        return result.i128.hi.int64 << 64 | result.i128.lo.uint64
    except Exception:
        return 0


async def get_swap_quote(
    token_in: str,
    token_out: str,
    amount: int,
) -> dict | None:
    """
    Get swap quote from Aquarius find-path API.
    Returns dict with swap_chain_xdr and amount, or None on failure.
    """
    payload = {
        "token_in_address": token_in,
        "token_out_address": token_out,
        "amount": str(amount),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                settings.AQUARIUS_API_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=settings.AQUARIUS_API_TIMEOUT),
            ) as resp:
                if resp.status == 400:
                    text = await resp.text()
                    logger.warning(f"Aquarius API 400: {text}")
                    return None
                resp.raise_for_status()
                data = await resp.json()
                if data.get("success"):
                    return data
                return None
    except Exception as e:
        logger.error(f"Aquarius API error: {e}")
        return None


async def swap_xlm_to_token(
    server: SorobanServerAsync,
    kp: Keypair,
    token_out: str,
    xlm_amount: int,
    dry_run: bool,
) -> bool:
    """
    Swap XLM → token_out via Aquarius router swap_chained.
    Returns True on success.
    """
    if token_out == settings.XLM_CONTRACT_ID:
        return True  # no swap needed

    quote = await get_swap_quote(settings.XLM_CONTRACT_ID, token_out, xlm_amount)
    if not quote:
        logger.warning(f"  no swap quote XLM → {token_out}")
        return False

    swap_chain_xdr = quote.get("swap_chain_xdr")
    if not swap_chain_xdr:
        logger.warning(f"  no swap_chain_xdr in quote")
        return False

    output_amount = int(quote["amount"])
    min_out = output_amount * (10_000 - settings.SWAP_SLIPPAGE_BPS) // 10_000

    logger.info(
        f"  swap XLM {xlm_amount} → ~{output_amount} {token_out[:12]}.. "
        f"min_out={min_out}"
    )

    builder = await aget_transaction_builder(server, kp.public_key)
    tx = (
        builder.append_invoke_contract_function_op(
            contract_id=settings.AMM_ROUTER,
            function_name="swap_chained",
            parameters=[
                scval.to_address(kp.public_key),
                SCVal.from_xdr(swap_chain_xdr),
                scval.to_address(settings.XLM_CONTRACT_ID),
                scval.to_uint128(xlm_amount),
                scval.to_uint128(min_out),
            ],
        )
        .build()
    )

    if dry_run:
        sim = await server.simulate_transaction(tx)
        ok = not sim.error
        logger.info(f"  [DRY] swap: {'OK' if ok else sim.error}")
        return ok

    result = await submit_transaction(server, tx, source_account=kp)
    return result is not None


async def ensure_tokens_for_amounts(
    server: SorobanServerAsync,
    kp: Keypair,
    needed: dict[str, int],
    dry_run: bool,
) -> bool:
    """
    Ensure admin has at least the specified amount of each token.
    needed: {token_address: min_amount}
    Swaps XLM → token if balance is insufficient.
    Returns True if all tokens are available.
    """
    all_ok = True
    for token, min_amount in needed.items():
        if min_amount <= 0:
            continue

        balance = await get_balance(server, kp.public_key, token)
        if balance >= min_amount:
            continue

        if token == settings.XLM_CONTRACT_ID:
            logger.warning(f"  insufficient XLM balance: {balance} < {min_amount}")
            all_ok = False
            continue

        logger.info(
            f"  {token[:12]}.. balance={balance} < {min_amount}, swapping XLM"
        )
        ok = await swap_xlm_to_token(
            server, kp, token, settings.SWAP_AMOUNT_XLM, dry_run
        )
        if not ok:
            all_ok = False

    return all_ok
