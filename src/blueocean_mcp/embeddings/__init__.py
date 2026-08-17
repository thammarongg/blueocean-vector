import os

from .base import Embedder


def create_embedder(provider: str | None = None) -> Embedder:
    """Instantiate an embedder based on the provider name.

    Defaults to FastEmbed (local, free, multilingual) so local and cloud
    deployments stay vector-compatible. Any provider can be selected with the
    ``BLUEOCEAN_EMBEDDING`` env var.

    Reads the env var fresh (rather than a module-level constant) so that
    setting ``os.environ["BLUEOCEAN_EMBEDDING"]`` at runtime -- e.g. from a
    CLI flag processed after this package was imported -- takes effect.
    """
    selected = provider or os.getenv("BLUEOCEAN_EMBEDDING", "fastembed")

    if selected == "fastembed":
        from .fastembed import FastEmbedEmbedder

        return FastEmbedEmbedder()
    if selected == "openai":
        from .openai import OpenAIEmbedder

        return OpenAIEmbedder()
    if selected == "bedrock":
        from .bedrock import BedrockEmbedder

        return BedrockEmbedder()

    raise ValueError(
        f"Unknown embedding provider: {selected}. "
        "Expected one of: fastembed, openai, bedrock."
    )


__all__ = ["Embedder", "create_embedder"]