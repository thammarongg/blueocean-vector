# Deploy the blueocean MCP server (and Qdrant config) as a container.
# FastEmbed weights are pinned at build time so the embedding model is the same
# in local dev and in the cloud, keeping vectors compatible.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install build tooling first for caching.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel && \
    pip install .

# Pre-download the default embedding model into the image so runtime is
# offline. Neither this nor FastEmbedEmbedder pass a cache_dir (unless
# BLUEOCEAN_EMBED_CACHE_DIR is set), so both land in fastembed's own default
# (/tmp/fastembed_cache) -- deliberately left unset here so build-time and
# runtime agree on the same path. /tmp is world-readable (1777) and the
# files fastembed writes are 0644/0755, so the non-root user below can read
# the pre-downloaded model without any extra chown.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='intfloat/multilingual-e5-large')"

# Run as a non-root user: this process handles untrusted network input.
# huggingface_hub also wants to touch lock/tree-cache bookkeeping files
# under the model cache on load (not just read the model itself), so that
# needs to be writable too, not just readable, or it logs (harmless but
# noisy) "Ignoring corrupted tree cache file: Permission denied" warnings.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app && \
    chmod -R a+rwX /tmp/fastembed_cache
USER appuser

EXPOSE 8765

ENTRYPOINT ["blueocean-mcp"]
# Default to streamable-http bound to all interfaces so the container's
# published port is reachable from the host / other containers. Override
# via docker-compose `command:` or `docker run ... blueocean-mcp <args>`.
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765", "--qdrant-url", "http://qdrant:6333"]