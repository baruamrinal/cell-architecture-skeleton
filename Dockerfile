# syntax=docker/dockerfile:1
#
# One image for every component; the command picks which one runs:
#   control plane: uvicorn cellarch.control_plane.app:create_app        --factory ...
#   router:        uvicorn cellarch.data_plane.router.app:create_app    --factory ...
#   cell:          uvicorn cellarch.data_plane.cell.app:create_app      --factory ...  (default)
#
# Base image pinned by digest (security guardrail, docs/plan.md §10). The digest is
# the multi-arch index, so the same pin builds for amd64 and arm64 (Graviton).
# Update it deliberately, e.g. via Dependabot.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels .

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data \
 && chown app /data

# Non-root; /data is the only writable path (run with a read-only root filesystem).
USER 10001
WORKDIR /data
EXPOSE 8000
CMD ["uvicorn", "cellarch.data_plane.cell.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
