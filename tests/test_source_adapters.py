"""Tests for the Source Adapter Layer (Deliverable 5, Feature 1 /
`app/services/source_adapters.py`)."""
from unittest.mock import patch

import pytest

from app.services.aggregator_service import FacebookPageConfig, TelegramChannelConfig
from app.services.source_adapters import (
    ADAPTERS,
    FacebookSourceAdapter,
    RawDiscoveryResult,
    TelegramSourceAdapter,
    UnsupportedSourceKindError,
    get_adapter,
)


class TestTelegramAdapter:
    def test_fetch_normalizes_into_raw_discovery_results(self):
        config = TelegramChannelConfig(handle="@testchannel", category="Scholarships")
        with patch(
            "app.services.source_adapters.fetch_new_telegram_posts",
            return_value=["post one", "post two"],
        ):
            results = TelegramSourceAdapter().fetch(config)
        assert results == [
            RawDiscoveryResult(raw_text="post one", source_kind="telegram_channel"),
            RawDiscoveryResult(raw_text="post two", source_kind="telegram_channel"),
        ]

    def test_fetch_empty_result(self):
        config = TelegramChannelConfig(handle="@testchannel", category="Scholarships")
        with patch("app.services.source_adapters.fetch_new_telegram_posts", return_value=[]):
            assert TelegramSourceAdapter().fetch(config) == []


class TestFacebookAdapter:
    def test_fetch_normalizes_into_raw_discovery_results(self):
        config = FacebookPageConfig(page_id="TestPage", name="Test Page", target_age="university")
        with patch(
            "app.services.source_adapters.fetch_new_facebook_posts",
            return_value=["fb post"],
        ):
            results = FacebookSourceAdapter().fetch(config)
        assert results == [RawDiscoveryResult(raw_text="fb post", source_kind="facebook_page")]

    def test_fetch_propagates_adapter_failure(self):
        """A malformed/failing fetch propagates as a real exception --
        the adapter never silently swallows a failure into an empty
        success."""
        config = FacebookPageConfig(page_id="TestPage", name="Test Page")
        with patch(
            "app.services.source_adapters.fetch_new_facebook_posts",
            side_effect=RuntimeError("timeout"),
        ):
            with pytest.raises(RuntimeError):
                FacebookSourceAdapter().fetch(config)


class TestAdapterRegistry:
    def test_get_adapter_known_kinds(self):
        assert isinstance(get_adapter("telegram_channel"), TelegramSourceAdapter)
        assert isinstance(get_adapter("facebook_page"), FacebookSourceAdapter)

    def test_get_adapter_unsupported_kind_raises(self):
        with pytest.raises(UnsupportedSourceKindError):
            get_adapter("rss_feed")

    def test_registry_has_exactly_the_three_real_configured_kinds(self):
        # Phase 3B added "telegram_channel_live" (LiveTelegramSourceAdapter)
        # alongside the two pre-existing, still-mocked kinds -- see
        # source_adapters.py's own module docstring.
        assert set(ADAPTERS.keys()) == {"telegram_channel", "telegram_channel_live", "facebook_page"}
