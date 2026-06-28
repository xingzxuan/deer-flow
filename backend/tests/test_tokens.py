"""Tests for deerflow.auth.tokens (Stage 1 PR1).

API key 格式 / 哈希 / prefix 截取。格式锁定 dfk_{live,test}_<24>，
prefix = 前 16 字符（含 dfk_live_），sha256 hex 存储（spec D5）。
"""

from __future__ import annotations

import hashlib

import pytest

from deerflow.auth.tokens import GeneratedKey, generate_api_key, hash_api_key, split_prefix


def test_generate_live_key_shape():
    key = generate_api_key("live")
    assert isinstance(key, GeneratedKey)
    assert key.plaintext.startswith("dfk_live_")
    # dfk_live_ (9) + token_urlsafe(18) (24) = 33 chars
    assert len(key.plaintext) == 33
    assert key.prefix == key.plaintext[:16]
    assert len(key.prefix) == 16
    assert key.key_hash == hashlib.sha256(key.plaintext.encode("utf-8")).hexdigest()
    assert len(key.key_hash) == 64


def test_generate_test_key_prefix_env():
    key = generate_api_key("test")
    assert key.plaintext.startswith("dfk_test_")
    assert len(key.plaintext) == 33
    assert key.prefix.startswith("dfk_test_")


def test_generate_rejects_bad_env():
    with pytest.raises(ValueError):
        generate_api_key("prod")  # type: ignore[arg-type]


def test_two_keys_are_unique():
    a = generate_api_key("live")
    b = generate_api_key("live")
    assert a.plaintext != b.plaintext
    assert a.key_hash != b.key_hash


def test_hash_api_key_is_sha256_and_deterministic():
    plaintext = "dfk_live_abcdefghijklmnopqrstuvwx"
    h1 = hash_api_key(plaintext)
    h2 = hash_api_key(plaintext)
    assert h1 == h2
    assert h1 != plaintext
    assert len(h1) == 64


def test_split_prefix_takes_first_16():
    assert split_prefix("dfk_live_abcdefghijklmnop") == "dfk_live_abcdefg"
