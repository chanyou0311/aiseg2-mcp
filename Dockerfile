# Container image for the AiSEG2 MCP server. Two-stage uv build; the dependency layer caches until
# pyproject.toml / uv.lock change. Built from the repo root (context: .).
#
# The image defaults to the streamable-http transport because a container is normally run as a
# long-lived network service (behind an authenticating proxy). stdio users should instead run the
# published PyPI package with `uvx aiseg2-mcp`.
#
# Note: .git is not in the build context, so hatch-vcs falls back to version 0.0.0 inside the image
# (the real version is carried by the image tag). Pass SETUPTOOLS_SCM_PRETEND_VERSION at build time
# to override if an accurate __version__ is needed in the container.

# --- build stage -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependency layer: manifests only, so it caches until deps change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Source layer: copy the package and install it into the venv.
COPY aiseg2_mcp/ aiseg2_mcp/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime stage ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm
LABEL io.modelcontextprotocol.server.name="io.github.chanyou0311/aiseg2-mcp"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    AISEG_TRANSPORT=streamable-http
# Run unprivileged. USER must be NUMERIC: Kubernetes runAsNonRoot cannot verify a non-root user
# from a name and refuses to start the container, so reference the UID.
RUN useradd -u 10001 -m app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "aiseg2_mcp"]
