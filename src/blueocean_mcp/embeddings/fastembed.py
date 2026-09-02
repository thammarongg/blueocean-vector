import os

from .base import Embedder


class FastEmbedEmbedder(Embedder):
    """Default embedder using FastEmbed with a local model.

    ``intfloat/multilingual-e5-large`` is multilingual (supports Thai + English)
    and produces 1024-dimensional vectors. It runs entirely on-device, so local
    and cloud deployments can pin the same model version and produce compatible
    vectors.
    """

    name = "fastembed"

    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding

        self._model_name = model or os.getenv(
            "BLUEOCEAN_EMBED_MODEL", "intfloat/multilingual-e5-large"
        )
        # cache_dir pins the exact downloaded weights so the same model is used
        # everywhere; dimension stays consistent across local and cloud.
        self._cache_dir = os.getenv("BLUEOCEAN_EMBED_CACHE_DIR")
        self._embedding_model = TextEmbedding(
            model_name=self._model_name,
            cache_dir=self._cache_dir,
        )

    @property
    def dimension(self) -> int:
        return int(self._embedding_model.embedding_size)

    def embed(self, texts: list[str]) -> list[list[float]]:
        import time

        from ..telemetry import usage
        from ..token_budget import estimate_tokens

        started = time.perf_counter()
        vectors = [list(v) for v in self._embedding_model.embed(texts)]
        # Local and free, so there is no real count to report. Estimating and
        # flagging it inexact keeps measured and guessed numbers separable
        # instead of summing them and implying equal precision.
        usage.add(
            sum(estimate_tokens(t) for t in texts),
            (time.perf_counter() - started) * 1000,
            exact=False,
        )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        # E5 models need a "query: " prefix for good retrieval quality; this
        # fastembed version does not add it automatically.
        return self.embed([f"query: {text}"])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed([f"passage: {t}" for t in texts])
