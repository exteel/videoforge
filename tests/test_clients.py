"""Unit tests for API clients — VoidAI, VoiceAPI, WaveSpeed."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_http_status_error(status_code: int, text: str = "error") -> httpx.HTTPStatusError:
    """Build a realistic httpx.HTTPStatusError with a mocked response."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError(
        message=f"HTTP {status_code}",
        request=request,
        response=response,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# VoidAI Client
# ═══════════════════════════════════════════════════════════════════════════════

class TestVoidAIFinishReason:
    """Tests for last_finish_reason tracking after chat_completion."""

    @pytest.mark.asyncio
    async def test_finish_reason_stop_is_recorded(self):
        """After a successful chat_completion, last_finish_reason is set from the API response."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            api_response = {
                "choices": [
                    {"message": {"content": "test response"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            client._post = AsyncMock(return_value=api_response)

            result = await client.chat_completion(
                "gpt-4.1-nano",
                [{"role": "user", "content": "hello"}],
            )

            assert result == "test response"
            assert client.last_finish_reason == "stop"

            await client.close()

    @pytest.mark.asyncio
    async def test_finish_reason_null_recorded(self):
        """last_finish_reason is None when API omits finish_reason."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            api_response = {
                "choices": [{"message": {"content": "hello"}}],  # no finish_reason key
                "usage": {},
            }
            client._post = AsyncMock(return_value=api_response)

            await client.chat_completion(
                "gpt-4.1-nano",
                [{"role": "user", "content": "ping"}],
            )

            assert client.last_finish_reason is None

            await client.close()

    @pytest.mark.asyncio
    async def test_finish_reason_length_logs_warning(self, caplog):
        """When finish_reason is 'length', a warning is emitted about truncation."""
        import logging

        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            api_response = {
                "choices": [
                    {"message": {"content": "truncated..."}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 4096},
            }
            client._post = AsyncMock(return_value=api_response)

            with caplog.at_level(logging.WARNING, logger="voidai"):
                await client.chat_completion(
                    "claude-opus-4-6",
                    [{"role": "user", "content": "write a long essay"}],
                    max_tokens=4096,
                )

            assert client.last_finish_reason == "length"
            # Warning must reference "length" so callers know output was cut off
            assert any("length" in record.message for record in caplog.records)

            await client.close()

    @pytest.mark.asyncio
    async def test_finish_reason_updated_on_successive_calls(self):
        """last_finish_reason reflects the MOST RECENT call, not a stale value."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            first_response = {
                "choices": [{"message": {"content": "first"}, "finish_reason": "stop"}],
                "usage": {},
            }
            second_response = {
                "choices": [{"message": {"content": "second"}, "finish_reason": "length"}],
                "usage": {},
            }
            client._post = AsyncMock(side_effect=[first_response, second_response])

            await client.chat_completion("gpt-4.1-nano", [{"role": "user", "content": "1"}])
            assert client.last_finish_reason == "stop"

            await client.chat_completion("gpt-4.1-nano", [{"role": "user", "content": "2"}])
            assert client.last_finish_reason == "length"

            await client.close()


class TestVoidAIFallbackChain:
    """Tests for the model fallback chain on failure."""

    @pytest.mark.asyncio
    async def test_falls_back_to_next_model_on_failure(self):
        """When the primary model raises, chat_completion retries with the fallback model."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient, FALLBACK_CHAIN

            client = VoidAIClient(api_key="test-key")
            await client.open()

            fallback_model = FALLBACK_CHAIN["claude-opus-4-6"]
            assert fallback_model is not None, "claude-opus-4-6 must have a fallback entry"

            success_response = {
                "choices": [{"message": {"content": "fallback answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }

            # First call (primary model) raises; second call (fallback) succeeds.
            client._post = AsyncMock(
                side_effect=[RuntimeError("primary model unavailable"), success_response]
            )

            result = await client.chat_completion(
                "claude-opus-4-6",
                [{"role": "user", "content": "test"}],
                use_fallback=True,
            )

            assert result == "fallback answer"
            assert client._post.call_count == 2

            # Verify the second call used the fallback model, not the original
            second_call_payload = client._post.call_args_list[1][0][1]
            assert second_call_payload["model"] == fallback_model

            await client.close()

    @pytest.mark.asyncio
    async def test_no_fallback_when_disabled(self):
        """With use_fallback=False, failure raises immediately without trying the chain."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            client._post = AsyncMock(side_effect=RuntimeError("model down"))

            with pytest.raises(RuntimeError, match="model down"):
                await client.chat_completion(
                    "claude-opus-4-6",
                    [{"role": "user", "content": "test"}],
                    use_fallback=False,
                )

            assert client._post.call_count == 1  # no retry

            await client.close()

    @pytest.mark.asyncio
    async def test_raises_when_entire_chain_exhausted(self):
        """Raises if the primary model AND all fallbacks fail."""
        with patch.dict("os.environ", {"VOIDAI_API_KEY": "test-key"}):
            from clients.voidai_client import VoidAIClient

            client = VoidAIClient(api_key="test-key")
            await client.open()

            # Every call fails — covers primary + any number of fallbacks
            client._post = AsyncMock(side_effect=RuntimeError("all models down"))

            with pytest.raises(RuntimeError):
                await client.chat_completion(
                    "gpt-4.1",  # gpt-4.1 has no further fallback (None in FALLBACK_CHAIN)
                    [{"role": "user", "content": "test"}],
                    use_fallback=True,
                )

            await client.close()

    def test_fallback_chain_structure(self):
        """Sanity-check: gpt-4.1 is the last resort and has no fallback."""
        from clients.voidai_client import FALLBACK_CHAIN

        assert FALLBACK_CHAIN["gpt-4.1"] is None
        assert FALLBACK_CHAIN["claude-opus-4-6"] is not None
        assert FALLBACK_CHAIN["claude-sonnet-4-5-20250929"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# VoiceAPI Client
# ═══════════════════════════════════════════════════════════════════════════════

class TestVoiceAPIContentTypeCheck:
    """Tests for the content-type OR logic in _fetch_result."""

    @pytest.mark.asyncio
    async def test_non_audio_content_type_raises_even_with_large_body(self):
        """
        application/json content-type must raise RuntimeError regardless of body size.

        The check is: 'audio' not in content_type OR size < 100.
        Large body alone should NOT suppress the error when MIME type is wrong.
        """
        with patch.dict("os.environ", {"VOICEAPI_KEY": "test-key"}):
            from clients.voiceapi_client import VoiceAPIClient

            client = VoiceAPIClient(api_key="test-key")
            await client.open()

            # Build a mock response with non-audio content-type and 5000 bytes of body
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.headers = {"content-type": "application/json"}
            mock_response.content = b"x" * 5000

            client._http.get = AsyncMock(return_value=mock_response)

            with pytest.raises(RuntimeError, match="content-type"):
                await client._fetch_result("task-123")

            await client.close()

    @pytest.mark.asyncio
    async def test_audio_content_type_does_not_raise(self):
        """audio/mpeg content-type with sufficient body returns bytes successfully."""
        with patch.dict("os.environ", {"VOICEAPI_KEY": "test-key"}):
            from clients.voiceapi_client import VoiceAPIClient

            client = VoiceAPIClient(api_key="test-key")
            await client.open()

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.headers = {"content-type": "audio/mpeg"}
            mock_response.content = b"\xff\xfb" + b"\x00" * 5000  # fake MP3 header + padding

            client._http.get = AsyncMock(return_value=mock_response)

            result = await client._fetch_result("task-456")

            assert isinstance(result, bytes)
            assert len(result) > 100

            await client.close()

    @pytest.mark.asyncio
    async def test_audio_content_type_with_small_body_raises(self):
        """audio/mpeg but body < 100 bytes must also raise (truncated/empty file)."""
        with patch.dict("os.environ", {"VOICEAPI_KEY": "test-key"}):
            from clients.voiceapi_client import VoiceAPIClient

            client = VoiceAPIClient(api_key="test-key")
            await client.open()

            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.headers = {"content-type": "audio/mpeg"}
            mock_response.content = b"\xff\xfb" * 10  # only 20 bytes — too small

            client._http.get = AsyncMock(return_value=mock_response)

            with pytest.raises(RuntimeError):
                await client._fetch_result("task-789")

            await client.close()


class TestVoiceAPIDoneStatuses:
    """Tests for the _DONE_STATUSES constant and its behavior in polling."""

    def test_ending_not_in_done_statuses(self):
        """
        'ending' must NOT be a terminal done status.

        The VoiceAPI state machine goes: processing → ending → (result available).
        Treating 'ending' as done causes _fetch_result to be called prematurely
        while the audio is still being finalized server-side.
        """
        from clients.voiceapi_client import _DONE_STATUSES

        assert "ending" not in _DONE_STATUSES, (
            "'ending' is an intermediate state, not terminal — "
            "polling must continue until 'done'/'completed'/etc."
        )

    def test_expected_statuses_are_in_done_set(self):
        """The required terminal statuses are present."""
        from clients.voiceapi_client import _DONE_STATUSES

        required = {"done", "completed", "finished", "success"}
        missing = required - _DONE_STATUSES
        assert not missing, f"Missing expected done statuses: {missing}"

    def test_error_statuses_are_separate(self):
        """Error statuses live in _ERROR_STATUSES, not _DONE_STATUSES."""
        from clients.voiceapi_client import _DONE_STATUSES, _ERROR_STATUSES

        # No overlap between done and error sets
        overlap = _DONE_STATUSES & _ERROR_STATUSES
        assert not overlap, f"Statuses appear in both sets: {overlap}"

    @pytest.mark.asyncio
    async def test_poll_continues_on_ending_status(self):
        """
        _poll_status must keep polling when status is 'ending'.

        Simulate: first response = 'ending', second = 'done'.
        The method should not return early on 'ending'.
        """
        with patch.dict("os.environ", {"VOICEAPI_KEY": "test-key"}):
            from clients.voiceapi_client import VoiceAPIClient

            client = VoiceAPIClient(api_key="test-key")
            await client.open()

            ending_response = MagicMock()
            ending_response.raise_for_status = MagicMock()
            ending_response.json = MagicMock(return_value={"status": "ending"})

            done_response = MagicMock()
            done_response.raise_for_status = MagicMock()
            done_response.json = MagicMock(return_value={"status": "done"})

            client._http.get = AsyncMock(side_effect=[ending_response, done_response])

            # Should complete without error — two polls: ending → done
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await client._poll_status("task-abc")

            assert client._http.get.call_count == 2

            await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# WaveSpeed Client
# ═══════════════════════════════════════════════════════════════════════════════

class TestWaveSpeedPollingFatalErrors:
    """Tests for the fatal HTTP error behavior during polling."""

    @pytest.mark.asyncio
    async def test_401_during_polling_raises_immediately(self):
        """
        HTTP 401 during the poll loop must raise RuntimeError immediately.

        Without this fix, the client would swallow the auth error with a
        'continue' and spin until MAX_POLLS, wasting time and tokens.
        """
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            # Successful POST that returns a task_id
            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-xyz"}})
            client._http.post = AsyncMock(return_value=post_response)

            # Poll returns 401 Unauthorized
            poll_http_error = _make_http_status_error(401, "Unauthorized")
            client._http.get = AsyncMock(side_effect=httpx.HTTPStatusError(
                message="401",
                request=poll_http_error.request,
                response=poll_http_error.response,
            ))

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="fatal HTTP 401"):
                    await client._post_and_poll("/some/endpoint", {"prompt": "test"})

            # Must have stopped after the very first poll, not retried MAX_POLLS times
            assert client._http.get.call_count == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_403_during_polling_raises_immediately(self):
        """HTTP 403 (forbidden) during polling is also fatal."""
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-abc"}})
            client._http.post = AsyncMock(return_value=post_response)

            poll_error = _make_http_status_error(403, "Forbidden")
            client._http.get = AsyncMock(side_effect=httpx.HTTPStatusError(
                message="403",
                request=poll_error.request,
                response=poll_error.response,
            ))

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="fatal HTTP 403"):
                    await client._post_and_poll("/endpoint", {"prompt": "test"})

            assert client._http.get.call_count == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_404_during_polling_raises_immediately(self):
        """HTTP 404 (task not found) during polling is fatal — task no longer exists."""
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-gone"}})
            client._http.post = AsyncMock(return_value=post_response)

            poll_error = _make_http_status_error(404, "Not Found")
            client._http.get = AsyncMock(side_effect=httpx.HTTPStatusError(
                message="404",
                request=poll_error.request,
                response=poll_error.response,
            ))

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="fatal HTTP 404"):
                    await client._post_and_poll("/endpoint", {"prompt": "test"})

            assert client._http.get.call_count == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_500_during_polling_does_not_raise_immediately(self):
        """
        HTTP 500 is a transient server error — polling should continue, not abort.

        The 401/403/404 fast-fail logic must NOT apply to 5xx errors.
        """
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-500"}})
            client._http.post = AsyncMock(return_value=post_response)

            # First poll: 500, second poll: completed
            server_error = _make_http_status_error(500, "Internal Server Error")

            success_poll = MagicMock()
            success_poll.raise_for_status = MagicMock()
            success_poll.json = MagicMock(return_value={
                "data": {"status": "completed", "outputs": ["https://cdn.wavespeed.ai/result.png"]}
            })

            client._http.get = AsyncMock(side_effect=[
                httpx.HTTPStatusError(
                    message="500",
                    request=server_error.request,
                    response=server_error.response,
                ),
                success_poll,
            ])

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client._post_and_poll("/endpoint", {"prompt": "test"})

            assert result == "https://cdn.wavespeed.ai/result.png"
            assert client._http.get.call_count == 2  # recovered on second poll

            await client.close()


class TestWaveSpeedSuccessfulPolling:
    """Tests for the happy-path polling completion logic."""

    @pytest.mark.asyncio
    async def test_returns_first_output_url_on_completed(self):
        """A poll response with status='completed' and outputs returns the first URL."""
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            expected_url = "https://cdn.wavespeed.ai/images/abc123.png"

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-ok"}})
            client._http.post = AsyncMock(return_value=post_response)

            poll_response = MagicMock()
            poll_response.raise_for_status = MagicMock()
            poll_response.json = MagicMock(return_value={
                "data": {
                    "status": "completed",
                    "outputs": [expected_url, "https://cdn.wavespeed.ai/images/alt.png"],
                }
            })
            client._http.get = AsyncMock(return_value=poll_response)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client._post_and_poll("/endpoint", {"prompt": "sunset"})

            assert result == expected_url

            await client.close()

    @pytest.mark.asyncio
    async def test_sync_response_skips_polling(self):
        """If POST response already contains outputs, polling is skipped entirely."""
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            sync_url = "https://cdn.wavespeed.ai/sync/result.png"

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={
                "data": {
                    "id": "task-sync",
                    "outputs": [sync_url],
                }
            })
            client._http.post = AsyncMock(return_value=post_response)
            client._http.get = AsyncMock()  # must not be called

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await client._post_and_poll("/endpoint", {"prompt": "test"})

            assert result == sync_url
            client._http.get.assert_not_called()

            await client.close()

    @pytest.mark.asyncio
    async def test_failed_status_raises_runtime_error(self):
        """A poll response with status='failed' raises RuntimeError with the error detail."""
        with patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}):
            from clients.wavespeed_client import WaveSpeedClient

            client = WaveSpeedClient(api_key="test-key")
            await client.open()

            post_response = MagicMock()
            post_response.raise_for_status = MagicMock()
            post_response.json = MagicMock(return_value={"data": {"id": "task-fail"}})
            client._http.post = AsyncMock(return_value=post_response)

            poll_response = MagicMock()
            poll_response.raise_for_status = MagicMock()
            poll_response.json = MagicMock(return_value={
                "data": {"status": "failed", "error": "NSFW content detected"}
            })
            client._http.get = AsyncMock(return_value=poll_response)

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(RuntimeError, match="NSFW content detected"):
                    await client._post_and_poll("/endpoint", {"prompt": "test"})

            await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CostBudget
# ═══════════════════════════════════════════════════════════════════════════════

class TestCostBudget:
    """Tests for the CostBudget.check() alerting and enforcement logic."""

    def test_no_limit_never_raises_or_warns(self, caplog):
        """CostBudget with no limit set is always a no-op check."""
        from pipeline import CostBudget

        budget = CostBudget(limit=None, spent=9999.0)

        import logging
        with caplog.at_level(logging.WARNING):
            budget.check()  # must not raise

        assert not caplog.records

    def test_under_80_percent_no_warning(self, caplog):
        """Spending below 80% of limit emits no warning."""
        from pipeline import CostBudget

        import logging
        budget = CostBudget(limit=10.0, spent=7.9)  # 79%

        with caplog.at_level(logging.WARNING):
            budget.check()

        assert not any("warning" in r.message.lower() for r in caplog.records)

    def test_at_80_percent_emits_warning(self, caplog):
        """Spending at exactly 80% of limit triggers a cost warning log."""
        from pipeline import CostBudget

        import logging
        budget = CostBudget(limit=10.0, spent=8.0)  # exactly 80%

        with caplog.at_level(logging.WARNING, logger="videoforge"):
            budget.check()

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected a WARNING log at 80% budget threshold"

    def test_warning_emitted_only_once(self, caplog):
        """The 80% warning fires at most once — subsequent check() calls are silent."""
        from pipeline import CostBudget

        import logging
        budget = CostBudget(limit=10.0, spent=8.5)  # 85%

        with caplog.at_level(logging.WARNING, logger="videoforge"):
            budget.check()
            budget.check()
            budget.check()

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "Budget warning" in r.message
        ]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 budget warning, got {len(warning_records)}"
        )

    def test_at_100_percent_raises_runtime_error(self):
        """Spending at or above the limit raises RuntimeError."""
        from pipeline import CostBudget

        budget = CostBudget(limit=5.0, spent=5.0)  # exactly at limit

        with pytest.raises(RuntimeError, match="Budget exceeded"):
            budget.check()

    def test_over_budget_raises_runtime_error(self):
        """Spending over the limit also raises RuntimeError."""
        from pipeline import CostBudget

        budget = CostBudget(limit=5.0, spent=5.01)

        with pytest.raises(RuntimeError, match="Budget exceeded"):
            budget.check()

    def test_progress_callback_called_at_80_percent(self):
        """At 80%, check() calls the optional progress_callback with a cost_warning event."""
        from pipeline import CostBudget

        callback = MagicMock()
        budget = CostBudget(limit=10.0, spent=8.0)

        budget.check(progress_callback=callback)

        callback.assert_called_once()
        event = callback.call_args[0][0]
        assert event["type"] == "cost_warning"
        assert event["spent"] == 8.0
        assert event["limit"] == 10.0
        assert event["pct"] == 80.0

    def test_progress_callback_not_called_when_under_threshold(self):
        """Progress callback is NOT invoked when spend is below 80%."""
        from pipeline import CostBudget

        callback = MagicMock()
        budget = CostBudget(limit=10.0, spent=7.0)  # 70%

        budget.check(progress_callback=callback)

        callback.assert_not_called()

    def test_no_limit_returns_false_from_over_budget(self):
        """over_budget() always returns False when no limit is configured."""
        from pipeline import CostBudget

        budget = CostBudget(limit=None, spent=1_000_000.0)
        assert budget.over_budget() is False

    def test_over_budget_returns_true_when_exceeded(self):
        """over_budget() returns True when spent > limit."""
        from pipeline import CostBudget

        budget = CostBudget(limit=1.0, spent=1.01)
        assert budget.over_budget() is True

    def test_over_budget_returns_false_when_at_limit(self):
        """over_budget() returns False at exactly the limit (strictly greater-than semantics)."""
        from pipeline import CostBudget

        budget = CostBudget(limit=1.0, spent=1.0)
        # check() raises at >= limit, but over_budget() uses strict >
        assert budget.over_budget() is False
