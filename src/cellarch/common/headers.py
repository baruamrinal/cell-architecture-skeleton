"""HTTP header names shared by all components."""

# Partition key. In production it is derived from the auth token or hostname at the edge.
TENANT_HEADER = "X-Tenant-ID"

# Stamped by every cell on every response.
CELL_HEADER = "X-Cell-ID"

# Service credential for control-plane APIs and cell /internal endpoints.
# Never accepted from clients: the router strips it.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"  # noqa: S105 - a header name, not a secret

REQUEST_ID_HEADER = "X-Request-ID"

# Name of the person or system calling the admin API (sent by cellctl). Recorded as
# created_by / updated_by next to the verified credential role. Self-reported locally;
# replaced by the verified IAM identity on AWS (stage 5).
ACTOR_HEADER = "X-Actor"
