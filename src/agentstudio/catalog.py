"""The tool catalog: typed tools an agent spec may use, and the systems behind them.

Specs never contain code. They reference tools by name, and the platform owns what those
tools do, what arguments they take, and whether they change anything. The systems here
are in-memory simulations of a directory, a ticketing system, an order system, payments
and a knowledge base for a fictional company.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolError(Exception):
    """A tool refused or failed; the message is safe to show to the agent and in logs."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UserArgs(_Args):
    user_id: str = Field(pattern=r"^U-[0-9]{3}$")


class TicketArgs(_Args):
    user_id: str = Field(pattern=r"^U-[0-9]{3}$")
    summary: str = Field(min_length=3, max_length=500)
    priority: Literal["low", "normal", "high"] = "normal"


class OrderArgs(_Args):
    order_id: str = Field(pattern=r"^O-[0-9]{4}$")


class RefundArgs(_Args):
    order_id: str = Field(pattern=r"^O-[0-9]{4}$")
    amount: float = Field(gt=0, le=100_000)
    reason: Literal["damaged", "not_received", "changed_mind", "other"]


class SearchArgs(_Args):
    query: str = Field(min_length=2, max_length=200)


_USERS = {
    "U-100": {
        "id": "U-100",
        "name": "Asha Rao",
        "department": "Finance",
        "locked": True,
        "privileged": False,
        "status": "active",
    },
    "U-101": {
        "id": "U-101",
        "name": "Ben Okafor",
        "department": "Platform",
        "locked": True,
        "privileged": True,
        "status": "active",
    },
    "U-102": {
        "id": "U-102",
        "name": "Carla Mendes",
        "department": "Sales",
        "locked": False,
        "privileged": False,
        "status": "disabled",
    },
    "U-103": {
        "id": "U-103",
        "name": "Dev Patel",
        "department": "Support",
        "locked": False,
        "privileged": False,
        "status": "active",
    },
}
_ORDERS = {
    "O-5001": {"id": "O-5001", "customer": "Mila Novak", "total": 49.9, "status": "delivered"},
    "O-5002": {"id": "O-5002", "customer": "Jonas Berg", "total": 189.0, "status": "delivered"},
    "O-5003": {"id": "O-5003", "customer": "Ines Duarte", "total": 640.0, "status": "delivered"},
    "O-5004": {"id": "O-5004", "customer": "Theo Laurent", "total": 75.0, "status": "in_transit"},
}
_ARTICLES = [
    {"id": "KB-1", "title": "Connect to the VPN", "keywords": ["vpn", "remote", "connect"]},
    {"id": "KB-2", "title": "Set up multi-factor authentication", "keywords": ["mfa", "2fa", "authenticator"]},
    {"id": "KB-3", "title": "Request a new laptop", "keywords": ["laptop", "hardware", "device"]},
]


@dataclass
class Systems:
    """State of the simulated systems; one instance per run in tests, shared in the service."""

    users: dict[str, dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(_USERS))
    orders: dict[str, dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(_ORDERS))
    tickets: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    resets: list[str] = field(default_factory=list)
    unlocks: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def user(self, user_id: str) -> dict[str, Any]:
        u = self.users.get(user_id)
        if u is None:
            raise ToolError(f"user {user_id} not found")
        return u


def _lookup_user(s: Systems, a: UserArgs) -> dict[str, Any]:
    u = s.user(a.user_id)
    return {k: u[k] for k in ("id", "name", "department", "locked", "privileged", "status")}


def _unlock(s: Systems, a: UserArgs) -> dict[str, Any]:
    u = s.user(a.user_id)
    if u["status"] != "active":
        raise ToolError("account is disabled; unlocking is not possible")
    u["locked"] = False
    s.unlocks.append(a.user_id)
    return {"user_id": a.user_id, "locked": False}


def _reset(s: Systems, a: UserArgs) -> dict[str, Any]:
    u = s.user(a.user_id)
    if u["status"] != "active":
        raise ToolError("account is disabled; password reset is not possible")
    s.resets.append(a.user_id)
    return {"user_id": a.user_id, "reset_link_sent": True}


def _ticket(s: Systems, a: TicketArgs) -> dict[str, Any]:
    s.user(a.user_id)
    t = {"id": f"INC-{len(s.tickets) + 1:05d}", "user_id": a.user_id, "summary": a.summary, "priority": a.priority}
    s.tickets.append(t)
    return t


def _order(s: Systems, a: OrderArgs) -> dict[str, Any]:
    o = s.orders.get(a.order_id)
    if o is None:
        raise ToolError(f"order {a.order_id} not found")
    refunded = sum(r["amount"] for r in s.refunds if r["order_id"] == a.order_id)
    return {**o, "refunded": refunded}


def _refund(s: Systems, a: RefundArgs) -> dict[str, Any]:
    o = s.orders.get(a.order_id)
    if o is None:
        raise ToolError(f"order {a.order_id} not found")
    refunded = sum(r["amount"] for r in s.refunds if r["order_id"] == a.order_id)
    if refunded + a.amount > o["total"] + 1e-9:
        raise ToolError("refund would exceed the order total")
    r = {"id": f"RF-{len(s.refunds) + 1:04d}", "order_id": a.order_id, "amount": a.amount, "reason": a.reason}
    s.refunds.append(r)
    return r


def _search(s: Systems, a: SearchArgs) -> dict[str, Any]:
    words = set(a.query.lower().split())
    hits = [x for x in _ARTICLES if words & set(x["keywords"])]
    return {"results": [{"id": h["id"], "title": h["title"]} for h in hits]}


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    kind: Literal["read", "write"]
    args_model: type[_Args]
    handler: Callable[[Systems, Any], dict[str, Any]]

    @property
    def arg_names(self) -> set[str]:
        return set(self.args_model.model_fields)

    @property
    def required_args(self) -> set[str]:
        return {n for n, f in self.args_model.model_fields.items() if f.is_required()}

    def call(self, systems: Systems, args: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = self.args_model.model_validate(args)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "arguments"
            raise ToolError(f"invalid argument {where}: {first.get('msg', 'invalid')}") from exc
        with systems.lock:
            return self.handler(systems, parsed)

    def describe(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "kind": self.kind, "args": schema}


CATALOG: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef("directory.lookup_user", "Look up a user in the directory.", "read", UserArgs, _lookup_user),
        ToolDef("directory.unlock_account", "Unlock a locked account.", "write", UserArgs, _unlock),
        ToolDef("directory.reset_password", "Send a password reset link.", "write", UserArgs, _reset),
        ToolDef("tickets.create_ticket", "Open a service desk ticket.", "write", TicketArgs, _ticket),
        ToolDef("orders.lookup_order", "Look up an order and what was refunded.", "read", OrderArgs, _order),
        ToolDef("payments.issue_refund", "Refund part or all of an order.", "write", RefundArgs, _refund),
        ToolDef("kb.search", "Search knowledge base articles.", "read", SearchArgs, _search),
    ]
}
