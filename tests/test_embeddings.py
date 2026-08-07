import json

import httpx
import pytest

from mytube.embeddings import EmbeddingError, OpenRouterEmbedder, build_embedding_input


class TestBuildInput:
    def test_all_fields(self):
        text = build_embedding_input(
            "Title", "Chan", "long description here", ["a", "b"],
            max_description_chars=800,
        )
        assert text == "Title\nChannel: Chan\nlong description here\nTags: a, b"

    def test_truncation_and_whitespace(self):
        text = build_embedding_input(
            "T", None, "word " * 500, None, max_description_chars=20
        )
        lines = text.split("\n")
        assert len(lines) == 2
        assert len(lines[1]) <= 20

    def test_tag_cap(self):
        text = build_embedding_input(
            "T", None, None, [f"t{i}" for i in range(50)], max_tags=15
        )
        assert text.count(",") == 14  # 15 tags -> 14 commas

    def test_empty_fields(self):
        assert build_embedding_input(None, None, None, None) == ""


def make_embedder(handler, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterEmbedder(
        "test-key",
        "https://openrouter.example/api/v1",
        client=client,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


class TestEmbedBatch:
    def test_success_and_stats(self):
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ],
                    "usage": {"total_tokens": 7},
                },
            )

        result = make_embedder(handler).embed_batch("model-x", ["a", "b"])
        # out-of-order indices are re-sorted
        assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
        assert result.stats.total_tokens == 7
        assert result.stats.dims == 2
        assert result.stats.calls == 1
        assert seen["payload"] == {"model": "model-x", "input": ["a", "b"]}

    def test_retry_on_429_then_success(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}},
            )

        sleeps = []
        result = make_embedder(handler, sleeps).embed_batch("m", ["a"])
        assert len(attempts) == 2
        assert len(sleeps) == 1
        assert result.vectors == [[1.0]]

    def test_duplicate_indices_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0]},
                        {"index": 0, "embedding": [2.0]},
                    ],
                    "usage": {},
                },
            )

        with pytest.raises(EmbeddingError, match="indices"):
            make_embedder(handler).embed_batch("m", ["a", "b"])

    def test_nan_vectors_rejected(self):
        # raw text body: httpx's mock refuses to serialize NaN itself, but
        # a real provider can emit the (non-strict) NaN token and json.loads
        # happily parses it — that's exactly the case being defended against
        def handler(request):
            return httpx.Response(
                200,
                text='{"data": [{"index": 0, "embedding": [NaN, 1.0]}], "usage": {}}',
                headers={"content-type": "application/json"},
            )

        with pytest.raises(EmbeddingError, match="NaN"):
            make_embedder(handler).embed_batch("m", ["a"])

    def test_inconsistent_dims_rejected(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 2.0]},
                        {"index": 1, "embedding": [1.0]},
                    ],
                    "usage": {},
                },
            )

        with pytest.raises(EmbeddingError, match="dimensions"):
            make_embedder(handler).embed_batch("m", ["a", "b"])

    def test_http_date_retry_after_falls_back(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(
                    429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
                )
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}}
            )

        sleeps = []
        result = make_embedder(handler, sleeps).embed_batch("m", ["a"])
        assert result.vectors == [[1.0]]
        assert sleeps == [1.0]  # 2**0 exponential fallback, not a crash

    def test_shape_mismatch_raises(self):
        def handler(request):
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}}
            )

        with pytest.raises(EmbeddingError, match="mismatch"):
            make_embedder(handler).embed_batch("m", ["a", "b"])

    def test_400_raises_without_retry(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(400, text="bad model")

        with pytest.raises(EmbeddingError, match="HTTP 400"):
            make_embedder(handler).embed_batch("m", ["a"])
        assert len(attempts) == 1

    def test_empty_input_no_call(self):
        def handler(request):  # pragma: no cover - must not be reached
            raise AssertionError("no HTTP call expected")

        result = make_embedder(handler).embed_batch("m", [])
        assert result.vectors == []

    def test_missing_key_rejected(self):
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            OpenRouterEmbedder("")
