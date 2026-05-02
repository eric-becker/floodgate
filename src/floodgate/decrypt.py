"""AES-128-CTR decryption for Meshtastic default-key channels.

Mirrors the Meshtastic firmware reference implementation
(meshtastic/firmware  src/mesh/CryptoEngine.cpp). Custom per-channel keys are
intentionally out of scope — only the default PSK (`AQ==`) is supported.
"""

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 16-byte AES-128 key derived from the default Meshtastic PSK (single-byte 0x01).
# Source: meshtastic/python  meshtastic/util.py  DEFAULT_KEY constant.
DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")

_NONCE_LEN = 16


def build_nonce(packet_id: int, from_node: int) -> bytes:
    """Construct the 16-byte AES-CTR nonce per Meshtastic's documented layout.

    Bytes 0-7  : packet_id   (uint64 little-endian)
    Bytes 8-11 : from_node   (uint32 little-endian)
    Bytes 12-15: extra_nonce (uint32 little-endian, always 0 — unused by floodgate)
    """
    return (
        (packet_id & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        + (from_node & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
    )


def decrypt(ciphertext: bytes, packet_id: int, from_node: int,
            key: bytes = DEFAULT_KEY) -> bytes:
    """AES-128-CTR decrypt a Meshtastic encrypted payload.

    AES-CTR is symmetric: this same call also encrypts. With a wrong key
    the result is plausible-looking garbage — no exception is raised and
    the caller must validate (e.g. by parsing as a Data protobuf).
    """
    nonce = build_nonce(packet_id, from_node)
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    return cipher.decryptor().update(ciphertext)
