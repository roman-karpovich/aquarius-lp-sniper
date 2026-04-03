"""
Tier classification logic for dust LP positions.

Tier 1 — Edge Sniping: dust beyond outermost liquidity, capture 100% of LP surplus.
Tier 2 — Full Range: full-range dust when competition detected at edges.
Tier 3 — Skip: pool already has full-range LP from others.

De-escalation: FullRange → Edge after cooldown, with halved escalation threshold.
"""

from datetime import datetime, timezone

import settings
from pool_state import PoolInfo

MIN_TICK = settings.MIN_TICK
MAX_TICK = settings.MAX_TICK


def aligned_min_tick(spacing: int) -> int:
    """
    Mirrors Soroban full_range_ticks(): tick_lower = MIN_TICK - (MIN_TICK % spacing),
    then if tick_lower < MIN_TICK: tick_lower += spacing.
    Rust % is remainder (truncation toward zero), Python % is modulo.
    """
    # Rust remainder: math.remainder semantics
    rem = MIN_TICK - (MIN_TICK // spacing) * spacing  # C-style remainder
    tick_lower = MIN_TICK - rem
    if tick_lower < MIN_TICK:
        tick_lower += spacing
    return tick_lower


def aligned_max_tick(spacing: int) -> int:
    """Mirrors Soroban full_range_ticks(): tick_upper = MAX_TICK - (MAX_TICK % spacing)."""
    rem = MAX_TICK - (MAX_TICK // spacing) * spacing
    tick_upper = MAX_TICK - rem
    if tick_upper > MAX_TICK:
        tick_upper -= spacing
    return tick_upper


def full_range_ticks(spacing: int) -> tuple[int, int]:
    return aligned_min_tick(spacing), aligned_max_tick(spacing)


def edge_ticks(pool: PoolInfo) -> tuple[tuple[int, int], tuple[int, int]]:
    """
    Compute edge positions: one tick_spacing below lowest, one above highest.
    Returns ((lower_l, lower_u), (upper_l, upper_u)).
    """
    sp = pool.tick_spacing
    if pool.initialized_ticks:
        lowest = pool.initialized_ticks[0][0]
        highest = pool.initialized_ticks[-1][0]
    else:
        aligned = pool.current_tick - (pool.current_tick % sp)
        lowest = highest = aligned

    lower_l = max(lowest - sp, aligned_min_tick(sp))
    lower_u = lowest
    upper_l = highest
    upper_u = min(highest + sp, aligned_max_tick(sp))

    return (lower_l, lower_u), (upper_l, upper_u)


def classify_pool(
    pool: PoolInfo,
    our_ranges: list[tuple[int, int]],
    pool_meta: dict,
) -> str:
    """
    Returns "edge", "full_range", or "skip".

    our_ranges: on-chain positions from get_user_position_snapshot
    pool_meta: local state (edge_miss_count, escalation_threshold, full_range_since)
    """
    if not pool.initialized_ticks:
        return "edge"

    ticks = [t for t, _ in pool.initialized_ticks]
    fr_l, fr_u = full_range_ticks(pool.tick_spacing)

    # Check if full-range LP exists
    has_full_range = min(ticks) <= fr_l and max(ticks) >= fr_u
    our_has_fr = any(l <= fr_l and u >= fr_u for l, u in our_ranges)

    if has_full_range and not our_has_fr:
        return "skip"  # someone else covers full range, our dust adds nothing

    # Determine our current tier from on-chain positions
    current_tier = None
    if our_has_fr:
        current_tier = "full_range"
    elif our_ranges:
        current_tier = "edge"

    # Edge: check if we're still outermost
    if current_tier == "edge":
        our_min = min(l for l, _ in our_ranges)
        our_max = max(u for _, u in our_ranges)
        beyond = any(t < our_min for t in ticks) or any(t > our_max for t in ticks)
        if beyond:
            pool_meta["edge_miss_count"] = pool_meta.get("edge_miss_count", 0) + 1
            if pool_meta["edge_miss_count"] >= settings.EDGE_THRESHOLD_INITIAL:
                return "full_range"
        else:
            pool_meta["edge_miss_count"] = 0

    # Full range: check cooldown for de-escalation
    if current_tier == "full_range":
        fr_since = pool_meta.get("full_range_since")
        if fr_since:
            elapsed = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(fr_since)
            ).total_seconds()
            cooldown = pool_meta.get(
                "cooldown_secs", settings.FULL_RANGE_COOLDOWN_SECS
            )
            if elapsed >= cooldown:
                # De-escalate, but double cooldown for next time
                pool_meta["cooldown_secs"] = cooldown * 2
                return "edge"
            return "full_range"

    return "edge"
