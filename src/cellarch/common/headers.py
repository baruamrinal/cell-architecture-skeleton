"""HTTP header names shared by all components."""

# Partition key. In production it is derived from the auth token or hostname at the edge.
TENANT_HEADER = "X-Tenant-ID"

# Stamped by every cell on every response.
CELL_HEADER = "X-Cell-ID"

# Service credential for control-plane APIs and cell /internal endpoints.
# Never accepted from clients: the router strips it.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"  # noqa: S105 - a header name, not a secret

REQUEST_ID_HEADER = "X-Request-ID"
