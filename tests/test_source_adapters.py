"""Tests for the Source Adapter Layer (Deliverable 5, Feature 1 /
`app/services/source_adapters.py`)."""
from unittest.mock import patch

import pytest

from app.services.aggregator_service import FacebookPageConfig, TelegramChannelConfig
from app.services.source_adapters import (
    ADAPTERS,
    FacebookGraphAPISourceAdapter,
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


class TestFacebookGraphAPIAdapter:
    """Post-final-audit remediation (FINDING-MED-1): the real Graph API
    client, now actually wired into this adapter boundary."""

    def test_fetch_normalizes_into_raw_discovery_results_with_provenance(self):
        config = FacebookPageConfig(page_id="officialchevening", name="Chevening", live=True)
        import datetime as dt

        published_at = dt.datetime(2026, 9, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
        fake_posts = [
            {
                "external_item_id": "123_987",
                "item_url": "https://www.facebook.com/officialchevening/posts/987",
                "raw_text": "Fully-funded scholarship, apply now.",
                "published_at": published_at,
            }
        ]
        with patch(
            "app.services.source_adapters.fetch_facebook_page_posts_live",
            return_value=fake_posts,
        ):
            results = FacebookGraphAPISourceAdapter().fetch(config)

        assert len(results) == 1
        result = results[0]
        assert result.source_kind == "facebook_page_live"
        assert result.raw_text == "Fully-funded scholarship, apply now."
        assert result.external_item_id == "123_987"
        assert result.item_url == "https://www.facebook.com/officialchevening/posts/987"
        assert result.published_at == published_at
        assert result.retrieved_at is not None  # stamped as real "now" by the adapter itself

    def test_fetch_propagates_source_not_configured_error(self):
        """No credential configured -> the adapter does NOT catch or
        swallow it into an empty success, and does NOT fall back to
        the mocked FacebookSourceAdapter or to HTML scraping. Isolation
        of this failure is `_poll_source`'s job, not this adapter's."""
        from app.services.facebook_graph_client import SourceNotConfiguredError

        config = FacebookPageConfig(page_id="officialchevening", name="Chevening", live=True)
        with patch(
            "app.services.source_adapters.fetch_facebook_page_posts_live",
            side_effect=SourceNotConfiguredError("not configured"),
        ):
            with pytest.raises(SourceNotConfiguredError):
                FacebookGraphAPISourceAdapter().fetch(config)

    def test_fetch_propagates_transient_and_permanent_errors(self):
        from app.services.facebook_graph_client import FacebookGraphAPITransientError

        config = FacebookPageConfig(page_id="officialchevening", name="Chevening", live=True)
        with patch(
            "app.services.source_adapters.fetch_facebook_page_posts_live",
            side_effect=FacebookGraphAPITransientError("timeout"),
        ):
            with pytest.raises(FacebookGraphAPITransientError):
                FacebookGraphAPISourceAdapter().fetch(config)

    def test_fetch_empty_result(self):
        config = FacebookPageConfig(page_id="officialchevening", name="Chevening", live=True)
        with patch("app.services.source_adapters.fetch_facebook_page_posts_live", return_value=[]):
            assert FacebookGraphAPISourceAdapter().fetch(config) == []


class TestAdapterRegistry:
    def test_get_adapter_known_kinds(self):
        assert isinstance(get_adapter("telegram_channel"), TelegramSourceAdapter)
        assert isinstance(get_adapter("facebook_page"), FacebookSourceAdapter)
        assert isinstance(get_adapter("facebook_page_live"), FacebookGraphAPISourceAdapter)

    def test_get_adapter_unsupported_kind_raises(self):
        with pytest.raises(UnsupportedSourceKindError):
            get_adapter("rss_feed")

    def test_registry_has_exactly_the_four_real_configured_kinds(self):
        # Phase 3B added "telegram_channel_live" (LiveTelegramSourceAdapter).
        # Post-final-audit remediation (FINDING-MED-1) added
        # "facebook_page_live" (FacebookGraphAPISourceAdapter) -- see
        # source_adapters.py's own module docstring.
        assert set(ADAPTERS.keys()) == {
            "telegram_channel",
            "telegram_channel_live",
            "facebook_page",
            "facebook_page_live",
        }
