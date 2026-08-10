import asyncio
import logging

from deep_translator import GoogleTranslator
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# `deep_translator.GoogleTranslator` wraps Google Translate's free, public
# web-facing endpoint -- not Google Cloud's paid, officially supported
# Translation API. That's exactly what "free, no API key" buys here, and
# it comes with real tradeoffs worth being upfront about rather than
# glossing over:
#   - it's an unofficial, unversioned endpoint Google doesn't guarantee for
#     programmatic use -- it can change, rate-limit, or block without notice;
#   - the `deep-translator` package itself has seen very little maintenance
#     activity over the last year or so (per public package-health data) --
#     it works today, but there's no one actively keeping it working if
#     Google changes something on their end.
# Reasonable for this bootstrapping stage; worth graduating to an official,
# paid API (Google Cloud Translation, DeepL) once translation volume or
# reliability actually matters to the business, not just to an MVP demo.

# Google Translate's free web endpoint caps request length around 5000
# characters; staying comfortably under that avoids silent truncation or
# request failures on longer scraped descriptions.
_MAX_TRANSLATE_CHARS = 4500

_retry_translate = retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
)


@_retry_translate
def _translate_sync(text: str, source_language: str) -> str:
    """The actual blocking call. `deep_translator` is built on `requests`
    (synchronous HTTP) with no native async client -- there is nothing to
    `await` here by design."""
    return GoogleTranslator(source=source_language, target="en").translate(text)


async def translate_to_english(text: str, source_language: str = "auto") -> str:
    """Translate `text` to English, off the event loop.

    Since `deep_translator` is purely synchronous, calling it directly
    inside an `async def` would block the *entire* event loop for the
    duration of each HTTP round-trip to Google's endpoint -- every other
    request/task the process is handling would stall too.
    `asyncio.to_thread` runs the blocking call on a worker thread instead,
    so the event loop stays free to do other work while this waits on
    network I/O.

    Fails soft: on any error (network failure, rate limiting, an empty/
    malformed response) this logs a warning and returns the original text
    unchanged rather than raising. A translation hiccup should never block
    a downstream step like AI verification, and feeding the original
    (untranslated) text into that step is a better fallback than feeding
    it nothing at all.
    """
    if not text or not text.strip():
        return text

    truncated = text[:_MAX_TRANSLATE_CHARS]

    try:
        return await asyncio.to_thread(_translate_sync, truncated, source_language)
    except Exception:
        logger.warning(
            "Translation failed (source_language=%r, text_len=%d); passing "
            "original text through unchanged",
            source_language,
            len(truncated),
            exc_info=True,
        )
        return text
