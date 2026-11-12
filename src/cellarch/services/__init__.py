"""Tenant microservices. Each tenant runs its own instances (docs/plan.md §3.4).

* ``user_account``      - the tenant's users
* ``order_processing``  - the tenant's orders; checks users through user-account's API

Both store their data in the tenant's one database, each in its own namespace
(a schema and login per service on Postgres).
"""
