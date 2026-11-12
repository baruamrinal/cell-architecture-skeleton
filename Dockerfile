# syntax=docker/dockerfile:1
#
# One image per component (docs/plan.md §8.5), built from shared stages:
#
#   docker build --target router           -t cellarch/router:dev .
#   docker build --target control-plane    -t cellarch/control-plane:dev .
#   docker build --target cell-gateway     -t cellarch/cell-gateway:dev .
#   docker build --target user-account     -t cellarch/user-account:dev .
#   docker build --target order-processing -t cellarch/order-processing:dev .
#
# Each image contains only its own code, the shared library (cellarch.common) and the
# dependencies. `make up` builds all five and loads them into the local k3d clusters.
#
# Base image pinned by digest (security guardrail, docs/plan.md §10). The digest is
# the multi-arch index, so the same pin builds for amd64 and arm64 (Graviton).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# --- dependencies (wheels), built once and cached ------------------------------------
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels .

# --- hardened runtime base: dependencies only, non-root ------------------------------
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir $(ls /wheels/*.whl | grep -v '/cellarch-') \
 && rm -rf /wheels \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data \
 && chown app /data
COPY src/cellarch/__init__.py /app/cellarch/__init__.py
COPY src/cellarch/common /app/cellarch/common
# Non-root; /data is the only writable path (run with a read-only root filesystem).
USER 10001
WORKDIR /data
EXPOSE 8000

# --- per-component images ------------------------------------------------------------
FROM base AS router
COPY src/cellarch/data_plane/__init__.py /app/cellarch/data_plane/__init__.py
COPY src/cellarch/data_plane/router /app/cellarch/data_plane/router
CMD ["uvicorn", "cellarch.data_plane.router.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS control-plane
COPY src/cellarch/control_plane /app/cellarch/control_plane
CMD ["uvicorn", "cellarch.control_plane.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS cell-gateway
COPY src/cellarch/data_plane/__init__.py /app/cellarch/data_plane/__init__.py
COPY src/cellarch/data_plane/cell /app/cellarch/data_plane/cell
CMD ["uvicorn", "cellarch.data_plane.cell.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS user-account
COPY src/cellarch/services/__init__.py /app/cellarch/services/__init__.py
COPY src/cellarch/services/common /app/cellarch/services/common
COPY src/cellarch/services/user_account /app/cellarch/services/user_account
CMD ["uvicorn", "cellarch.services.user_account.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS order-processing
COPY src/cellarch/services/__init__.py /app/cellarch/services/__init__.py
COPY src/cellarch/services/common /app/cellarch/services/common
COPY src/cellarch/services/order_processing /app/cellarch/services/order_processing
CMD ["uvicorn", "cellarch.services.order_processing.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

