from abc import ABC, abstractmethod


class Embedder(ABC):
    """Interface for all embedding providers.

    Any provider must embed a batch of texts into fixed-size vectors.
    The embedding dimension is consistent within a provider/model so
    vectors written to Qdrant can be searched back correctly.

    Some models (notably the E5 family, e.g. ``intfloat/multilingual-e5-large``)
    are asymmetric: they require a ``"query: "`` prefix for search queries and
    a ``"passage: "`` prefix for stored documents to get good retrieval
    quality. ``embed_query``/``embed_documents`` let each provider apply its
    own prefixing; providers that don't need this can leave the defaults,
    which simply call ``embed``.
    """

    name: str

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into a list of vectors (no prefixing)."""
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        """Embed a single search query. Override to apply query-side prefixing."""
        return self.embed([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents to be stored. Override to apply passage-side prefixing."""
        return self.embed(texts)

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimension produced by this embedder."""
        raise NotImplementedError