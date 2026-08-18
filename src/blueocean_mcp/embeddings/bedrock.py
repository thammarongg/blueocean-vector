import json
import os
from typing import Any

from .base import Embedder

DEFAULT_MODEL_ID = "amazon.titan-embed-text-v2:0"


class BedrockEmbedder(Embedder):
    """Embedder backed by Amazon Bedrock Titan embeddings."""

    name = "bedrock"

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
    ) -> None:
        import boto3

        self._model_id = model_id or os.getenv(
            "BLUEOCEAN_BEDROCK_MODEL_ID",
            DEFAULT_MODEL_ID,
        )
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region or os.getenv("AWS_REGION", "us-east-1"),
        )

    @property
    def dimension(self) -> int:
        # amazon.titan-embed-text-v2:0 default output is 1024-dim.
        return 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            resp: dict[str, Any] = self._client.invoke_model(
                modelId=self._model_id,
                body=json.dumps({"inputText": text}),
                contentType="application/json",
                accept="application/json",
            )
            body = resp["body"].read().decode("utf-8")
            vectors.append(json.loads(body)["embedding"])
        return vectors