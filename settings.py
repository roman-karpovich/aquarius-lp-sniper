import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

prod = os.getenv("PROD", "true").lower() in ("true", "1", "yes")

_secret_path = Path(__file__).parent / "secret.json"
if _secret_path.exists():
    with open(_secret_path, "r") as f:
        config = json.load(f)
else:
    config = {}

# Network
SOROBAN_RPC_URL = os.getenv(
    "SOROBAN_RPC_URL",
    "http://localhost:8000/" if prod else "https://soroban-rpc.ultrastellar.com/",
)

# Contracts
SOROBAN_BATCHER_ADDRESS = "CBZX5A64HWVYXGGXSSWGYZZTUYFNGVKLAESK3XOZDJXYKLOY7MTCFAEV"
AMM_ROUTER = "CBQDHNBFBZYE4MKPWBSJOPIYLW4SFSXAXUTSXJN76GNKYVYPCKWC6QUK"

BASE_FEE = 1_000_000

# Token contracts (mainnet)
XLM_CONTRACT_ID = "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA"

# Aquarius API
AQUARIUS_API_URL = "https://amm-api.aqua.network/api/external/v1/find-path/"
AQUARIUS_API_TIMEOUT = 10

# Swap config
SWAP_SLIPPAGE_BPS = int(os.getenv("DUST_LP_SWAP_SLIPPAGE_BPS", "100"))  # 1%
SWAP_AMOUNT_XLM = int(os.getenv("DUST_LP_SWAP_AMOUNT", "10000000"))  # 1 XLM per token swap

# Sniper-specific
EDGE_THRESHOLD_INITIAL = int(os.getenv("DUST_LP_EDGE_THRESHOLD", "3"))
FULL_RANGE_COOLDOWN_SECS = int(os.getenv("DUST_LP_FR_COOLDOWN", "300"))
POLL_INTERVAL_SECS = int(os.getenv("DUST_LP_POLL_INTERVAL", "30"))

MIN_TICK = -887_272
MAX_TICK = 887_272
