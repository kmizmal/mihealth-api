# -*- coding: utf-8 -*-
"""签名/加密层：RC4(drop=1024)、nonce、sessionKey、两个签名分支。"""
import hashlib
import struct
import time

import pytest

from mihealth_api import client as C


def _ref_rc4(key: bytes) -> "object":
    from Crypto.Cipher import ARC4
    c = ARC4.new(key)
    c.encrypt(b"\x00" * 1024)          # drop 前 1024 字节
    return c


def test_b64_roundtrip():
    assert C.b64d(C.b64e(b"hello")) == b"hello"


def test_rc4_matches_pycryptodome():
    key = bytes(range(32))
    payload = b"the quick brown fox" * 7
    assert C.RC4(key).crypt(payload) == _ref_rc4(key).encrypt(payload)


def test_rc4_pure_python_fallback_matches(monkeypatch):
    key = b"\x5a" * 32
    expected = _ref_rc4(key).encrypt(b"payload-bytes")
    monkeypatch.setattr(C, "_ARC4", None)      # 强制走纯 Python PRGA
    assert C.RC4(key).crypt(b"payload-bytes") == expected


def test_rc4_streaming_is_chunk_invariant():
    """分块加解密与一次性处理必须等价（真实调用是逐参数加密的）。"""
    key = b"\x11" * 32
    whole = C.RC4(key).crypt(b"aaaabbbbcccc")
    chunky = C.RC4(key)
    assert chunky.crypt(b"aaaa") + chunky.crypt(b"bbbb") + chunky.crypt(b"cccc") == whole


def test_rc4_rejects_wrong_key_length():
    with pytest.raises(ValueError):
        C.RC4(b"short")


def test_session_key_is_sha256_of_concat():
    sec, nonce = C.b64e(b"a" * 16), C.b64e(b"b" * 12)
    assert C.b64d(C.session_key(sec, nonce)) == hashlib.sha256(b"a" * 16 + b"b" * 12).digest()


def test_gen_nonce_layout():
    raw = C.b64d(C.gen_nonce())
    assert len(raw) == 12                                  # 8B random + int32 minutes
    minutes = struct.unpack(">i", raw[8:])[0]
    assert abs(minutes - int(time.time() * 1000 // 60000)) <= 1


def test_gen_nonce_time_diff_shifts_minutes():
    raw = C.b64d(C.gen_nonce(5 * 60000))
    minutes = struct.unpack(">i", raw[8:])[0]
    assert abs(minutes - (int(time.time() * 1000 // 60000) + 5)) <= 1


def _decrypt_params(out: dict, nonce: str, security: str) -> dict:
    """按服务端视角解密：RC4 流按 sorted(key) 顺序推进。"""
    rc4 = C.RC4(C.b64d(C.session_key(security, nonce)))
    plain = {}
    for k in sorted(out):
        if k in ("signature", "_nonce"):
            continue
        plain[k] = rc4.crypt(C.b64d(out[k])).decode("utf-8")
    return plain


def test_signed_encrypted_roundtrip_and_signature():
    sec, nonce = C.b64e(b"s" * 16), C.gen_nonce()
    params = {"data": '{"key":"steps"}', "empty": "", "none": None}
    out = C.signed_encrypted("GET", "/app/v1/data/get_fitness_data_by_time", params, nonce, sec)

    assert out["_nonce"] == nonce
    plain = _decrypt_params(out, nonce, sec)
    assert plain["data"] == '{"key":"steps"}'
    assert "empty" not in plain and "none" not in plain      # 空值被剔除
    assert "rc4_hash__" in plain                             # hash 也被加密

    skey = C.session_key(sec, nonce)
    path = "/app/v1/data/get_fitness_data_by_time"
    expect_hash = hashlib.sha1("&".join(
        ["GET", path] + [f"{k}={v}" for k, v in sorted(
            {"data": '{"key":"steps"}'}.items())] + [skey]).encode()).digest()
    assert plain["rc4_hash__"] == C.b64e(expect_hash)

    enc = {k: out[k] for k in sorted(out) if k not in ("signature", "_nonce")}
    expect_sig = hashlib.sha1("&".join(
        ["GET", path] + [f"{k}={v}" for k, v in enc.items()] + [skey]).encode()).digest()
    assert out["signature"] == C.b64e(expect_sig)


def test_signed_plain_hmac():
    sec, nonce = C.b64e(b"k" * 16), C.gen_nonce()
    out = C.signed_plain("GET", "/app/v1/x",
                         {"data": "{}", "blank": ""}, nonce, sec)
    assert out["_nonce"] == nonce and "blank" not in out
    skey = C.session_key(sec, nonce)
    expect = C.b64e(__import__("hmac").new(
        C.b64d(skey), "&".join(["GET", skey, nonce, "data={}"]).encode(),
        hashlib.sha256).digest())
    assert out["signature"] == expect


def test_decrypt_response_roundtrip():
    sec, nonce = C.b64e(b"r" * 16), C.gen_nonce()
    body = '{"code":0,"result":{"data_list":[]}}'
    enc = C.b64e(C.RC4(C.b64d(C.session_key(sec, nonce))).crypt(body.encode()))
    assert C.decrypt_response(enc, nonce, sec) == body


def test_path_of_strips_prefix():
    assert C._path_of("/app/v1/data/x?y=1") == "/app/v1/data/x"
    assert C._path_of("data/x") == "data/x"
