"""
Aquarius concentrated pool state fetching — slot0, bitmap scanning, tick batch fetch.
Mirrors the Rust aquarius_concentrated.rs logic.
"""

from dataclasses import dataclass
from itertools import batched

from loguru import logger
from stellar_sdk import Address, SorobanServerAsync, scval
from stellar_sdk.xdr import SCVal

from args_conversion import i128_to_int, raw_contract_id_to_address, u128_to_int
from tx_builder import aget_transaction_builder
from tx_simulate import asimulate_transaction_results

import settings

TICKS_PER_CHUNK = 16
BITMAP_WORDS_PER_CALL = 64
TICKS_BATCH_SIZE = 50


@dataclass
class PoolInfo:
    address: str
    tokens: list[str]
    tick_spacing: int
    current_tick: int
    liquidity: int
    initialized_ticks: list[tuple[int, int]]  # sorted (tick, liquidity_net)
    min_init_tick: int
    max_init_tick: int


# --- Batcher helpers ---


def build_call(contract_addr: str, method: str, args: list) -> SCVal:
    return scval.to_vec([
        scval.to_address(contract_addr),
        scval.to_symbol(method),
        scval.to_vec(args),
    ])


async def simulate_batched(
    server: SorobanServerAsync,
    public_key: str,
    calls: list[SCVal],
) -> list[SCVal]:
    if not calls:
        return []

    builder = await aget_transaction_builder(server, public_key)
    tx = builder.append_invoke_contract_function_op(
        contract_id=settings.SOROBAN_BATCHER_ADDRESS,
        function_name="batch",
        parameters=[
            scval.to_vec([]),
            scval.to_vec(calls),
            scval.to_bool(True),
        ],
    ).build()

    result = await asimulate_transaction_results(server, tx)
    if result.vec and result.vec.sc_vec:
        return list(result.vec.sc_vec)
    return []


# --- XDR parsing ---


def parse_i32(val: SCVal) -> int:
    return val.i32.int32


def parse_i128(val: SCVal) -> int:
    return i128_to_int(val.i128)


def parse_u128(val: SCVal) -> int:
    return u128_to_int(val.u128)


def parse_vec(val: SCVal) -> list[SCVal]:
    if val.vec and val.vec.sc_vec:
        return list(val.vec.sc_vec)
    return []


def parse_map(val: SCVal) -> dict[str, SCVal]:
    if val.map and val.map.sc_map:
        return {e.key.sym.sc_symbol.decode(): e.val for e in val.map.sc_map}
    return {}


# --- Pool discovery ---


async def discover_concentrated_pools(
    server: SorobanServerAsync,
    public_key: str,
) -> list[str]:
    """
    Query Aquarius router to enumerate all pools, filter for concentrated type.
    Returns list of concentrated pool contract addresses.
    """
    # Get total token sets count
    tx = (
        (await aget_transaction_builder(server, public_key))
        .append_invoke_contract_function_op(
            contract_id=settings.AMM_ROUTER,
            function_name="get_tokens_sets_count",
            parameters=[],
        )
        .build()
    )
    result = await asimulate_transaction_results(server, tx)
    total = parse_u128(result)
    logger.info(f"router has {total} token sets")

    # Enumerate all pools
    all_pool_addrs = []
    for indices in batched(range(total), 10):
        tx = (
            (await aget_transaction_builder(server, public_key))
            .append_invoke_contract_function_op(
                contract_id=settings.AMM_ROUTER,
                function_name="get_pools_for_tokens_range",
                parameters=[
                    scval.to_uint128(indices[0]),
                    scval.to_uint128(indices[-1] + 1),
                ],
            )
            .build()
        )
        result = await asimulate_transaction_results(server, tx)
        for entry in result.vec.sc_vec:
            pools_map = entry.vec.sc_vec[1].map.sc_map
            for pool_entry in pools_map:
                pool_addr = raw_contract_id_to_address(
                    pool_entry.val.address.contract_id.hash.hex()
                )
                all_pool_addrs.append(pool_addr)

    logger.info(f"found {len(all_pool_addrs)} total pools, checking types...")

    # Filter for concentrated pools via get_info()
    concentrated = []
    for pool_batch in batched(all_pool_addrs, 5):
        calls = [build_call(addr, "get_info", []) for addr in pool_batch]
        try:
            results = await simulate_batched(server, public_key, calls)
        except Exception as e:
            logger.warning(f"batch get_info failed: {e}")
            continue

        for addr, info_scval in zip(pool_batch, results):
            try:
                info = parse_map(info_scval)
                pool_type = info.get("pool_type")
                if pool_type and pool_type.sym.sc_symbol.decode() == "concentrated":
                    concentrated.append(addr)
            except Exception as e:
                logger.debug(f"  {addr}: failed to parse info: {e}")

    logger.info(f"found {len(concentrated)} concentrated pools")
    return concentrated


# --- Pool state fetching ---


async def fetch_pool_info(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
) -> tuple[int, int, int, int, int, list[str]]:
    """
    Returns (tick, liquidity, min_init_tick, max_init_tick, tick_spacing, tokens).
    """
    calls = [
        build_call(pool_address, "get_slot0", []),
        build_call(pool_address, "get_active_liquidity", []),
        build_call(pool_address, "get_tick_bounds", []),
        build_call(pool_address, "get_tick_spacing", []),
        build_call(pool_address, "get_tokens", []),
    ]

    results = await simulate_batched(server, public_key, calls)
    if len(results) < 5:
        raise RuntimeError(f"expected 5 results, got {len(results)}")

    # slot0: map { sqrt_price_x96: U256, tick: i32 }
    slot0 = parse_map(results[0])
    tick = parse_i128(slot0["tick"])

    # active liquidity: u128
    liquidity = parse_i128(results[1])

    # tick bounds: vec [min, max]
    bounds = parse_vec(results[2])
    min_init = parse_i128(bounds[0]) if len(bounds) >= 1 else -887_272
    max_init = parse_i128(bounds[1]) if len(bounds) >= 2 else 887_272

    # tick spacing: positive integer
    tick_spacing = parse_i128(results[3])

    # tokens: vec of addresses
    tokens_vec = parse_vec(results[4])
    tokens = [
        raw_contract_id_to_address(t.address.contract_id.hash.hex())
        for t in tokens_vec
    ]

    return tick, liquidity, min_init, max_init, tick_spacing, tokens


async def scan_chunk_bitmap(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
    bounds: tuple[int, int],
    tick_spacing: int,
) -> list[int]:
    """Scan chunk bitmap to find initialized chunk positions."""
    min_compressed = bounds[0] // tick_spacing if bounds[0] >= 0 else -((-bounds[0] + tick_spacing - 1) // tick_spacing)
    max_compressed = bounds[1] // tick_spacing if bounds[1] >= 0 else -((-bounds[1] + tick_spacing - 1) // tick_spacing)

    # Python div_euclid equivalent
    def div_euclid(a: int, b: int) -> int:
        q = a // b
        if a % b != 0 and (a ^ b) < 0:
            q -= 1
        return q

    min_compressed = div_euclid(bounds[0], tick_spacing)
    max_compressed = div_euclid(bounds[1], tick_spacing)
    min_chunk = div_euclid(min_compressed, TICKS_PER_CHUNK)
    max_chunk = div_euclid(max_compressed, TICKS_PER_CHUNK)
    start_word = min_chunk >> 8
    end_word = max_chunk >> 8

    chunk_positions = []
    current_word = start_word

    while current_word <= end_word:
        remaining = end_word - current_word + 1
        count = min(remaining, BITMAP_WORDS_PER_CALL)

        results = await simulate_batched(
            server,
            public_key,
            [build_call(
                pool_address,
                "get_chunk_bitmap_batch",
                [scval.to_int32(current_word), scval.to_uint32(count)],
            )],
        )

        if not results:
            current_word += count
            continue

        words = parse_vec(results[0])
        for offset, word_scval in enumerate(words):
            word_pos = current_word + offset
            # Parse U256: lo_lo, lo_hi, hi_lo, hi_hi (each u64)
            parts = word_scval.u256
            limbs = [
                parts.lo_lo.uint64,
                parts.lo_hi.uint64,
                parts.hi_lo.uint64,
                parts.hi_hi.uint64,
            ]
            for limb_idx, limb in enumerate(limbs):
                while limb != 0:
                    bit = (limb & -limb).bit_length() - 1
                    chunk_pos = (word_pos << 8) + bit + limb_idx * 64
                    chunk_positions.append(chunk_pos)
                    limb &= limb - 1

        current_word += count

    return chunk_positions


async def fetch_ticks_for_chunks(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
    chunk_positions: list[int],
    tick_spacing: int,
) -> list[tuple[int, int]]:
    """Fetch tick data for initialized chunks, filter by liquidity_gross > 0."""
    # Build tick indices from chunks
    tick_indices = []
    for chunk_pos in chunk_positions:
        for slot in range(TICKS_PER_CHUNK):
            compressed = chunk_pos * TICKS_PER_CHUNK + slot
            tick = compressed * tick_spacing
            tick_indices.append(tick)

    if not tick_indices:
        return []

    result_ticks = []
    for chunk in batched(tick_indices, TICKS_BATCH_SIZE):
        chunk = list(chunk)
        args = scval.to_vec([scval.to_int32(t) for t in chunk])

        results = await simulate_batched(
            server,
            public_key,
            [build_call(pool_address, "get_ticks_batch", [args])],
        )

        if not results:
            continue

        infos = parse_vec(results[0])
        if len(infos) != len(chunk):
            logger.warning(
                f"ticks_batch mismatch: expected {len(chunk)}, got {len(infos)}"
            )
            continue

        for i, info_scval in enumerate(infos):
            tick = chunk[i]
            info = parse_map(info_scval)
            liquidity_gross = parse_i128(info["liquidity_gross"])
            if liquidity_gross > 0:
                liquidity_net = parse_i128(info["liquidity_net"])
                result_ticks.append((tick, liquidity_net))

    result_ticks.sort(key=lambda x: x[0])
    # dedup by tick
    seen = set()
    deduped = []
    for t, ln in result_ticks:
        if t not in seen:
            seen.add(t)
            deduped.append((t, ln))
    return deduped


# --- Full refresh ---


async def refresh_pool(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
) -> PoolInfo:
    """Full refresh: fetch pool info → bitmap → ticks → PoolInfo."""
    tick, liquidity, min_init, max_init, tick_spacing, tokens = await fetch_pool_info(
        server, public_key, pool_address
    )

    chunk_positions = await scan_chunk_bitmap(
        server, public_key, pool_address, (min_init, max_init), tick_spacing
    )

    initialized_ticks = await fetch_ticks_for_chunks(
        server, public_key, pool_address, chunk_positions, tick_spacing
    )

    logger.debug(
        f"pool {pool_address}: tick={tick} liq={liquidity} "
        f"ticks={len(initialized_ticks)} spacing={tick_spacing}"
    )

    return PoolInfo(
        address=pool_address,
        tokens=tokens,
        tick_spacing=tick_spacing,
        current_tick=tick,
        liquidity=liquidity,
        initialized_ticks=initialized_ticks,
        min_init_tick=min_init,
        max_init_tick=max_init,
    )


async def estimate_min_deposit(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
    tick_lower: int,
    tick_upper: int,
) -> list[int] | None:
    """
    Find minimum deposit amounts that produce liquidity > 0
    via estimate_deposit_position simulation.

    Binary searches between 1 and upper bound.
    Returns [amount0, amount1] or None if even large amounts fail.
    """
    # Phase 1: find an upper bound that works (powers of 10)
    hi = None
    for exp in range(1, 20):  # 10^1 .. 10^19
        amount = 10 ** exp
        try:
            results = await simulate_batched(
                server,
                public_key,
                [build_call(
                    pool_address,
                    "estimate_deposit_position",
                    [
                        scval.to_int32(tick_lower),
                        scval.to_int32(tick_upper),
                        scval.to_vec([scval.to_uint128(amount), scval.to_uint128(amount)]),
                    ],
                )],
            )
            if results:
                hi = amount
                break
        except Exception:
            continue

    if hi is None:
        logger.warning(
            f"  estimate_deposit_position failed for all amounts "
            f"[{tick_lower},{tick_upper}]"
        )
        return None

    # Phase 2: binary search for minimum
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            results = await simulate_batched(
                server,
                public_key,
                [build_call(
                    pool_address,
                    "estimate_deposit_position",
                    [
                        scval.to_int32(tick_lower),
                        scval.to_int32(tick_upper),
                        scval.to_vec([scval.to_uint128(mid), scval.to_uint128(mid)]),
                    ],
                )],
            )
            if results:
                hi = mid
            else:
                lo = mid + 1
        except Exception:
            lo = mid + 1

    # Parse actual amounts from the winning estimate
    try:
        results = await simulate_batched(
            server,
            public_key,
            [build_call(
                pool_address,
                "estimate_deposit_position",
                [
                    scval.to_int32(tick_lower),
                    scval.to_int32(tick_upper),
                    scval.to_vec([scval.to_uint128(hi), scval.to_uint128(hi)]),
                ],
            )],
        )
        if not results:
            return [hi, hi]

        # Returns (actual_amounts: Vec<u128>, liquidity: u128)
        result_vec = parse_vec(results[0])
        actual_amounts_vec = parse_vec(result_vec[0])
        amounts = [parse_u128(a) for a in actual_amounts_vec]
        logger.debug(
            f"  min deposit [{tick_lower},{tick_upper}]: "
            f"input={hi} actual={amounts}"
        )
        return amounts
    except Exception as e:
        logger.warning(f"  final estimate parse failed: {e}")
        return [hi, hi]


async def get_our_positions(
    server: SorobanServerAsync,
    public_key: str,
    pool_address: str,
) -> list[tuple[int, int]]:
    """
    Call get_user_position_snapshot(owner) on the pool contract.
    Returns list of (tick_lower, tick_upper) for our positions.
    """
    results = await simulate_batched(
        server,
        public_key,
        [build_call(
            pool_address,
            "get_user_position_snapshot",
            [scval.to_address(public_key)],
        )],
    )

    if not results:
        return []

    # UserPositionSnapshot { ranges: Vec<PositionRange>, raw_liquidity, weighted_liquidity }
    snapshot = parse_map(results[0])
    ranges_vec = parse_vec(snapshot.get("ranges", SCVal.from_xdr(scval.to_vec([]).to_xdr())))

    positions = []
    for range_scval in ranges_vec:
        r = parse_map(range_scval)
        tick_lower = parse_i32(r["tick_lower"])
        tick_upper = parse_i32(r["tick_upper"])
        positions.append((tick_lower, tick_upper))

    return positions
