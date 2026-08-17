import os

from .base import Embedder

# Known dimensions per model, since Qdrant collections must be created with
# the exact vector size the model produces.
_KNOWN_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedder(Embedder):
    """Embedder backed by OpenAI's embeddings API."""

    name = "openai"

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str | None = None,
        dimension: int | None = None,
    ) -> None:
        from openai import OpenAI

        self._model = model
        if dimension is not None:
            self._dimension = dimension
        elif model in _KNOWN_DIMENSIONS:
            self._dimension = _KNOWN_DIMENSIONS[model]
        else:
            raise ValueError(
                f"Unknown dimension for OpenAI model '{model}'. "
                "Pass dimension=... explicitly."
            )
        self._client = OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        data = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in data]