from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

GUARD0_ORG_PARAMETER = "GUARD0_ORG_ID"

_guard0_org_id: ContextVar[str | None] = ContextVar(
    "cartography_guard0_org_id",
    default=None,
)
_guard0_scope_required: ContextVar[bool] = ContextVar(
    "cartography_guard0_scope_required",
    default=False,
)


def current_guard0_org_id() -> str:
    organization_id = _guard0_org_id.get()
    return organization_id or "cartography-default"


def guard0_scope_required() -> bool:
    return _guard0_scope_required.get()


def add_guard0_org_parameter(parameters: dict) -> dict:
    scoped = parameters.copy()
    scoped[GUARD0_ORG_PARAMETER] = current_guard0_org_id()
    return scoped


@contextmanager
def guard0_tenant_scope(organization_id: str) -> Iterator[None]:
    normalized = organization_id.strip()
    if not normalized:
        raise ValueError("guard0_org_id must be non-empty")
    token = _guard0_org_id.set(normalized)
    required_token = _guard0_scope_required.set(True)
    try:
        yield
    finally:
        _guard0_scope_required.reset(required_token)
        _guard0_org_id.reset(token)
