import os
from pathlib import Path

DEFAULT_QDRANT_URL = os.getenv("BLUEOCEAN_QDRANT_URL", "http://localhost:6333")
DEFAULT_EMBEDDING_PROVIDER = os.getenv("BLUEOCEAN_EMBEDDING", "fastembed")
DEFAULT_EMBED_MODEL = os.getenv("BLUEOCEAN_EMBED_MODEL", "intfloat/multilingual-e5-large")
DEFAULT_COLLECTION_PREFIX = os.getenv("BLUEOCEAN_COLLECTION_PREFIX", "blueocean")
DEFAULT_MAX_TOKENS = int(os.getenv("BLUEOCEAN_MAX_TOKENS", "2000"))
DEFAULT_TOP_K = int(os.getenv("BLUEOCEAN_TOP_K", "20"))

# Ceilings, not defaults: this is a shared server multiple agents hit at once,
# so a single misbehaving caller shouldn't be able to store unbounded payloads
# or force a wide-open Qdrant scan via an absurd top_k.
MAX_CONTENT_CHARS = int(os.getenv("BLUEOCEAN_MAX_CONTENT_CHARS", "100000"))
MAX_TOP_K = int(os.getenv("BLUEOCEAN_MAX_TOP_K", "200"))

DEFAULT_HNSW_M = int(os.getenv("BLUEOCEAN_HNSW_M", "16"))
DEFAULT_HNSW_EF_CONSTRUCT = int(os.getenv("BLUEOCEAN_HNSW_EF_CONSTRUCT", "100"))

# How long /health caches a cloud embedding provider's (openai/bedrock)
# credential self-test result. Without this, docker-compose.yml's 10s
# healthcheck interval would turn into a provider API call every 10s --
# fine for fastembed (local, free) but a real rate-limit/latency risk for
# a remote provider. fastembed itself never uses this: its model loads
# synchronously at server startup, so a broken load crashes before /health
# could ever be hit, making a repeated check pointless.
DEFAULT_HEALTH_EMBED_TTL = float(os.getenv("BLUEOCEAN_HEALTH_EMBED_TTL", "60"))

# Telemetry. Off means off: with BLUEOCEAN_TELEMETRY=0 no database file is
# ever opened, and the HTTP surface answers 503 rather than 404 -- a 404
# makes a deliberate configuration look like a broken deployment.
TELEMETRY_ENABLED = os.getenv("BLUEOCEAN_TELEMETRY", "1") != "0"

# Under docker-compose this is overridden to /data/telemetry.db, which is a
# bind mount. The default below is for running outside a container.
DEFAULT_TELEMETRY_DB = os.getenv(
    "BLUEOCEAN_TELEMETRY_DB", str(Path.home() / ".blueocean" / "telemetry.db")
)
DEFAULT_PRICING_FILE = os.getenv(
    "BLUEOCEAN_PRICING_FILE", str(Path.home() / ".blueocean" / "pricing.json")
)
TELEMETRY_RETENTION_DAYS = int(os.getenv("BLUEOCEAN_TELEMETRY_RETENTION_DAYS", "90"))
TELEMETRY_QUEUE_SIZE = int(os.getenv("BLUEOCEAN_TELEMETRY_QUEUE", "10000"))

# Where blueocean-admin looks for the server when it needs telemetry it is
# not allowed to read off disk (see the spec's single-writer rule).
DEFAULT_SERVER_URL = os.getenv("BLUEOCEAN_SERVER_URL", "http://localhost:8765")
