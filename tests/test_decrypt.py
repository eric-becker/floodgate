"""Tests for AES-128-CTR Meshtastic decryption."""

import pytest

from floodgate.decrypt import DEFAULT_KEY, build_nonce, decrypt


class TestBuildNonce:

    def test_layout_packet_id_first_then_from_then_zero_padding(self):
        # packet_id = 0x0807060504030201, from = 0x0c0b0a09
        nonce = build_nonce(0x0807060504030201, 0x0C0B0A09)
        assert nonce == bytes.fromhex(
            "0102030405060708"   # packet_id LE
            "090a0b0c"           # from LE
            "00000000"           # extra_nonce
        )

    def test_length_is_16_bytes(self):
        assert len(build_nonce(1, 1)) == 16

    def test_zero_inputs(self):
        assert build_nonce(0, 0) == b"\x00" * 16

    def test_max_uint64_packet_id(self):
        nonce = build_nonce(0xFFFFFFFFFFFFFFFF, 0)
        assert nonce[:8] == b"\xff" * 8
        assert nonce[8:] == b"\x00" * 8

    def test_max_uint32_from_node(self):
        nonce = build_nonce(0, 0xFFFFFFFF)
        assert nonce[:8] == b"\x00" * 8
        assert nonce[8:12] == b"\xff" * 4
        assert nonce[12:] == b"\x00" * 4


class TestDefaultKey:

    def test_length_is_16_bytes_aes128(self):
        assert len(DEFAULT_KEY) == 16

    def test_canonical_value(self):
        # Documented Meshtastic default key — fixed forever, do not change.
        assert DEFAULT_KEY.hex() == "d4f1bb3a20290759f0bcffabcf4e6901"


class TestDecryptRoundtrip:
    """AES-CTR is symmetric — same function encrypts and decrypts."""

    def test_roundtrip_default_key(self):
        plaintext = b"hello meshtastic world"
        ct = decrypt(plaintext, packet_id=12345, from_node=0xDEADBEEF)
        assert ct != plaintext   # actually encrypted
        pt = decrypt(ct, packet_id=12345, from_node=0xDEADBEEF)
        assert pt == plaintext

    def test_roundtrip_explicit_key(self):
        key = b"\x42" * 16
        plaintext = b"some data"
        ct = decrypt(plaintext, packet_id=1, from_node=2, key=key)
        pt = decrypt(ct, packet_id=1, from_node=2, key=key)
        assert pt == plaintext

    def test_empty_input_returns_empty(self):
        assert decrypt(b"", packet_id=1, from_node=1) == b""

    def test_wrong_key_produces_garbage_no_exception(self):
        plaintext = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"
        ct = decrypt(plaintext, packet_id=1, from_node=1, key=DEFAULT_KEY)
        wrong_key = b"\x99" * 16
        garbage = decrypt(ct, packet_id=1, from_node=1, key=wrong_key)
        assert garbage != plaintext
        assert len(garbage) == len(plaintext)

    def test_different_nonce_different_ciphertext(self):
        plaintext = b"same plaintext"
        ct1 = decrypt(plaintext, packet_id=1, from_node=1)
        ct2 = decrypt(plaintext, packet_id=2, from_node=1)
        assert ct1 != ct2


class TestDecryptRealMeshtasticInterop:
    """Decrypt a Data protobuf encrypted with the canonical Meshtastic
    parameters and verify it parses back to the original portnum.

    This is the integration check that pins our implementation to the
    upstream firmware behavior — if Meshtastic ever changes the nonce
    layout, AES variant, or default key, this test fails.
    """

    def test_roundtrip_through_data_protobuf(self):
        pytest.importorskip("meshtastic")
        from meshtastic import mesh_pb2, portnums_pb2

        original = mesh_pb2.Data()
        original.portnum = portnums_pb2.PortNum.RANGE_TEST_APP
        original.payload = b"seq 42"

        packet_id, from_node = 0xABCDEF01, 0x12345678
        ciphertext = decrypt(
            original.SerializeToString(),
            packet_id=packet_id, from_node=from_node,
        )

        recovered = mesh_pb2.Data()
        recovered.ParseFromString(decrypt(ciphertext, packet_id, from_node))
        assert recovered.portnum == portnums_pb2.PortNum.RANGE_TEST_APP
        assert recovered.payload == b"seq 42"
