"""The SessionStore seam: opaque login-session ids (BE-0015 7b).

`issue()` mints a fresh id (set as the `bajutsu_session` cookie at login); `valid()` checks one.
A session may carry an *identity* (the GitHub login from an OAuth login, BE-0015 7b-2) so a later
layer (RBAC, 7c) can map a session back to a user; a shared-token login carries none.
`InMemorySessionStore` is the local default — sessions live in this process, so a restart drops them
(re-login). The server backend swaps in a database-backed store (`SqlSessionStore`) so sessions
survive restarts and span control-plane processes; the seam keeps `ServeState` and the auth layer
unaware of which is in use."""

from .in_memory_session_store import InMemorySessionStore
from .principal import HUMAN, MACHINE, Principal, PrincipalKind, kind_from_stored
from .session_store import SessionStore

__all__ = [
    "HUMAN",
    "MACHINE",
    "InMemorySessionStore",
    "Principal",
    "PrincipalKind",
    "SessionStore",
    "kind_from_stored",
]
