#!/usr/bin/env python3
"""
Dust LP Sniper for Aquarius CLMM concentrated pools.

Places minimal liquidity positions to:
  - Edge sniping: capture surplus when swaps push past existing liquidity
  - Full range: extend price movement for arb bot when competition detected

Usage:
  python sniper.py --dry-run --once                    # simulate all pools once
  python sniper.py --pools CXXX CYYY --dry-run --once  # specific pools only
  python sniper.py                                     # continuous loop, all pools
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from stellar_sdk import Keypair, SorobanServerAsync, scval

import settings
from aquarius_swap import ensure_tokens_for_amounts
from pool_state import (
    PoolInfo,
    discover_concentrated_pools,
    estimate_min_deposit,
    get_our_positions,
    refresh_pool,
)
from tier_logic import classify_pool, edge_ticks, full_range_ticks
from tx_builder import aget_transaction_builder
from tx_submit import submit_transaction

logger.add("sniper.log", level="DEBUG", rotation="10 MB", retention="10 days")

STATE_FILE = Path(__file__).parent / "dust_lp_state.json"


# --- State persistence ---


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"pools": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# --- Contract calls ---


async def deposit_position(
    server: SorobanServerAsync,
    kp: Keypair,
    pool_addr: str,
    tick_lower: int,
    tick_upper: int,
    amounts: list[int],
    dry_run: bool,
) -> bool:
    builder = await aget_transaction_builder(server, kp.public_key)
    tx = (
        builder.append_invoke_contract_function_op(
            contract_id=pool_addr,
            function_name="deposit_position",
            parameters=[
                scval.to_address(kp.public_key),
                scval.to_int32(tick_lower),
                scval.to_int32(tick_upper),
                scval.to_vec([scval.to_uint128(a) for a in amounts]),
                scval.to_uint128(0),  # min_liquidity
            ],
        )
        .build()
    )

    if dry_run:
        sim = await server.simulate_transaction(tx)
        ok = not sim.error
        logger.info(
            f"  [DRY] deposit {pool_addr} [{tick_lower},{tick_upper}]: "
            f"{'OK' if ok else sim.error}"
        )
        return ok

    logger.info(f"  depositing {pool_addr} [{tick_lower},{tick_upper}] amounts={amounts}")
    result = await submit_transaction(server, tx, source_account=kp)
    return result is not None


async def withdraw_position(
    server: SorobanServerAsync,
    kp: Keypair,
    pool_addr: str,
    tick_lower: int,
    tick_upper: int,
    dry_run: bool,
) -> None:
    builder = await aget_transaction_builder(server, kp.public_key)
    tx = (
        builder.append_invoke_contract_function_op(
            contract_id=pool_addr,
            function_name="withdraw_position",
            parameters=[
                scval.to_address(kp.public_key),
                scval.to_int32(tick_lower),
                scval.to_int32(tick_upper),
                scval.to_uint128(2**128 - 1),  # withdraw all liquidity
                scval.to_vec([scval.to_uint128(0), scval.to_uint128(0)]),
            ],
        )
        .build()
    )

    if dry_run:
        sim = await server.simulate_transaction(tx)
        logger.info(
            f"  [DRY] withdraw {pool_addr} [{tick_lower},{tick_upper}]: "
            f"{'OK' if not sim.error else sim.error}"
        )
        return

    logger.info(f"  withdrawing {pool_addr} [{tick_lower},{tick_upper}]")
    await submit_transaction(server, tx, source_account=kp)


async def claim_fees(
    server: SorobanServerAsync,
    kp: Keypair,
    pool_addr: str,
    tick_lower: int,
    tick_upper: int,
    dry_run: bool,
) -> None:
    builder = await aget_transaction_builder(server, kp.public_key)
    tx = (
        builder.append_invoke_contract_function_op(
            contract_id=pool_addr,
            function_name="claim_position_fees",
            parameters=[
                scval.to_address(kp.public_key),
                scval.to_int32(tick_lower),
                scval.to_int32(tick_upper),
            ],
        )
        .build()
    )

    if dry_run:
        sim = await server.simulate_transaction(tx)
        logger.info(
            f"  [DRY] claim {pool_addr} [{tick_lower},{tick_upper}]: "
            f"{'OK' if not sim.error else sim.error}"
        )
        return

    logger.info(f"  claiming fees {pool_addr} [{tick_lower},{tick_upper}]")
    await submit_transaction(server, tx, source_account=kp)


# --- Handlers ---


async def handle_edge(
    server: SorobanServerAsync,
    kp: Keypair,
    pool: PoolInfo,
    our_ranges: list[tuple[int, int]],
    pool_meta: dict,
    dry_run: bool,
) -> None:
    (lower_l, lower_u), (upper_l, upper_u) = edge_ticks(pool)
    target_ranges = {(lower_l, lower_u), (upper_l, upper_u)}
    current_ranges = set(our_ranges)

    # Withdraw positions that don't match new edge targets
    for tl, tu in current_ranges - target_ranges:
        try:
            await withdraw_position(server, kp, pool.address, tl, tu, dry_run)
        except Exception as e:
            logger.warning(f"  withdraw ({tl},{tu}) failed: {e}")

    # Deposit positions we don't have yet
    for tl, tu in target_ranges - current_ranges:
        amounts = await estimate_min_deposit(
            server, kp.public_key, pool.address, tl, tu
        )
        if amounts is None:
            logger.warning(f"  cannot estimate min deposit for ({tl},{tu}), skipping")
            continue

        # Ensure we have enough tokens for the estimated amounts
        needed = dict(zip(pool.tokens, amounts))
        if not await ensure_tokens_for_amounts(
            server, kp, needed, dry_run
        ):
            logger.warning(f"  cannot acquire tokens for ({tl},{tu}), skipping")
            continue

        try:
            await deposit_position(server, kp, pool.address, tl, tu, amounts, dry_run)
        except Exception as e:
            logger.warning(f"  deposit ({tl},{tu}) failed: {e}")

    pool_meta["edge_miss_count"] = 0
    pool_meta["full_range_since"] = None
    pool_meta["last_rebalance"] = datetime.now(timezone.utc).isoformat()


async def handle_full_range(
    server: SorobanServerAsync,
    kp: Keypair,
    pool: PoolInfo,
    our_ranges: list[tuple[int, int]],
    pool_meta: dict,
    dry_run: bool,
) -> None:
    fr_l, fr_u = full_range_ticks(pool.tick_spacing)

    # Already have full range?
    if (fr_l, fr_u) in our_ranges:
        return

    amounts = await estimate_min_deposit(
        server, kp.public_key, pool.address, fr_l, fr_u
    )
    if amounts is None:
        logger.warning(f"  cannot estimate min deposit for full range, skipping")
        return

    # Ensure we have enough tokens for the estimated amounts
    needed = dict(zip(pool.tokens, amounts))
    if not await ensure_tokens_for_amounts(
        server, kp, needed, dry_run
    ):
        logger.warning(f"  cannot acquire tokens for full range, skipping")
        return

    # Withdraw all old positions (edges)
    for tl, tu in our_ranges:
        try:
            await withdraw_position(server, kp, pool.address, tl, tu, dry_run)
        except Exception as e:
            logger.warning(f"  withdraw ({tl},{tu}) failed: {e}")

    try:
        await deposit_position(server, kp, pool.address, fr_l, fr_u, amounts, dry_run)
    except Exception as e:
        logger.error(f"  full-range deposit failed: {e}")
        return

    pool_meta["full_range_since"] = pool_meta.get(
        "full_range_since"
    ) or datetime.now(timezone.utc).isoformat()
    pool_meta["last_rebalance"] = datetime.now(timezone.utc).isoformat()


# --- Main ---


async def run(dry_run: bool, once: bool, pool_filter: list[str] | None) -> None:
    kp = Keypair.from_secret(settings.config["admin"])
    state = load_state()

    async with SorobanServerAsync(settings.SOROBAN_RPC_URL) as server:
        while True:
            # Discover all concentrated pools from router
            try:
                all_pools = await discover_concentrated_pools(server, kp.public_key)
            except Exception as e:
                logger.error(f"pool discovery failed: {e}")
                if once:
                    return
                await asyncio.sleep(settings.POLL_INTERVAL_SECS)
                continue

            if pool_filter:
                all_pools = [p for p in all_pools if p in pool_filter]

            logger.info(f"processing {len(all_pools)} concentrated pools")

            for addr in all_pools:
                try:
                    pool = await refresh_pool(server, kp.public_key, addr)
                    our_ranges = await get_our_positions(server, kp.public_key, addr)
                except Exception as e:
                    logger.error(f"failed to refresh {addr}: {e}")
                    continue

                pool_meta = state["pools"].setdefault(addr, {})
                tier = classify_pool(pool, our_ranges, pool_meta)

                logger.info(
                    f"{addr}: tier={tier} ticks={len(pool.initialized_ticks)} "
                    f"spacing={pool.tick_spacing} tick={pool.current_tick} "
                    f"our_positions={len(our_ranges)}"
                )

                if tier == "skip":
                    logger.info(f"  skip — full-range LP exists from others")
                    continue

                try:
                    if tier == "edge":
                        await handle_edge(
                            server, kp, pool, our_ranges, pool_meta, dry_run
                        )
                    elif tier == "full_range":
                        await handle_full_range(
                            server, kp, pool, our_ranges, pool_meta, dry_run
                        )
                except Exception as e:
                    logger.error(f"  handler failed: {e}")

                # Claim fees from existing positions
                for tl, tu in our_ranges:
                    try:
                        await claim_fees(server, kp, addr, tl, tu, dry_run)
                    except Exception as e:
                        logger.debug(f"  claim ({tl},{tu}) failed: {e}")

            if not dry_run:
                save_state(state)

            if once:
                break

            logger.info(f"sleeping {settings.POLL_INTERVAL_SECS}s...")
            await asyncio.sleep(settings.POLL_INTERVAL_SECS)


def main():
    parser = argparse.ArgumentParser(
        description="Dust LP Sniper for Aquarius CLMM pools"
    )
    parser.add_argument(
        "--pools", nargs="*", help="Filter to specific pool addresses (default: all)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate only")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    asyncio.run(run(args.dry_run, args.once, args.pools))


if __name__ == "__main__":
    main()
