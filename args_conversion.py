import binascii

from stellar_sdk import StrKey, scval
from stellar_sdk.xdr import Int128Parts, SCAddressType, SCVal, UInt128Parts


def str_to_bytesn32(value: str) -> SCVal:
    return scval.to_bytes(bytes.fromhex(value))


def raw_contract_id_to_address(value: str) -> str:
    return StrKey.encode_contract(binascii.unhexlify(value))


def i128_to_int(value: Int128Parts) -> int:
    return int(value.hi.int64 << 64) + value.lo.uint64


def u128_to_int(value: UInt128Parts) -> int:
    return int(value.hi.uint64 << 64) + value.lo.uint64


def get_address_from_scval(value):
    if value.address.type == SCAddressType.SC_ADDRESS_TYPE_ACCOUNT:
        return StrKey.encode_ed25519_public_key(
            value.address.account_id.account_id.ed25519.uint256
        )
    else:
        return raw_contract_id_to_address(value.address.contract_id.hash.hex())
