from __future__ import annotations

from funding_bot.adapters.exchanges.x10.adapter import (
    _looks_like_x10_testnet,
    _normalize_x10_api_v1_base_url,
    _normalize_x10_onboarding_url,
)


def test_normalize_x10_api_v1_base_url_appends_api_v1():
    assert (
        _normalize_x10_api_v1_base_url("https://api.starknet.extended.exchange")
        == "https://api.starknet.extended.exchange/api/v1"
    )
    assert (
        _normalize_x10_api_v1_base_url("https://api.starknet.extended.exchange/api/v1")
        == "https://api.starknet.extended.exchange/api/v1"
    )
    assert (
        _normalize_x10_api_v1_base_url("https://api.starknet.extended.exchange/api/v1/")
        == "https://api.starknet.extended.exchange/api/v1"
    )


def test_normalize_x10_onboarding_url_strips_api_v1():
    assert (
        _normalize_x10_onboarding_url("https://api.starknet.extended.exchange/api/v1")
        == "https://api.starknet.extended.exchange"
    )
    assert (
        _normalize_x10_onboarding_url("https://api.starknet.extended.exchange/api/v1/")
        == "https://api.starknet.extended.exchange"
    )
    assert (
        _normalize_x10_onboarding_url("https://api.starknet.extended.exchange")
        == "https://api.starknet.extended.exchange"
    )


def test_looks_like_x10_testnet_detects_sepolia():
    assert _looks_like_x10_testnet("https://api.starknet.sepolia.extended.exchange") is True
    assert _looks_like_x10_testnet("https://api.starknet.extended.exchange") is False
