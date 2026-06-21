"""
Centralized deployable-capital policy (the single source of truth for "the most
capital the system may put to work at once").

Before this module, the maximum capital the bots could commit was implicit and
scattered:
  * spot trend-follower : ``portfolio.max_total_exposure_pct`` (a % of equity)
  * funding carry       : ``carry.capital.sleeve_usd``        (a fixed USD sleeve)
  * ETF momentum        : ``etf.capital.sleeve_usd`` + ``max_total_exposure_pct``

``DeployableCapitalPolicy`` unifies all of those into one strongly-typed,
validated value object. A policy answers exactly one question:

    given my current equity and available cash, what is the GROSS dollar
    envelope I am allowed to have committed at once?

It deliberately does NOT decide *which* opportunity gets the money or how a
single position is sized - the existing per-asset / per-buy allocation logic is
untouched. The policy only caps the *total envelope*; ranking and relative
weighting inside that envelope stay exactly as they were.

Financial terms used precisely throughout:
  * equity           - total account value (cash + marked-to-market holdings).
  * available cash   - free quote-currency balance not yet committed.
  * committed capital- capital already tied up in open positions.
  * deployable cap.  - the envelope this policy permits (a function of the above).
  * remaining cap.   - deployable - committed, floored at zero (and, optionally,
                       clamped to available cash so we never deploy money we do
                       not actually have).

All math is done in :class:`decimal.Decimal` so percentage-of-equity arithmetic
is exact and reproducible (no float drift on money).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

# What the percentage applies to.
_VALID_BASIS = ("equity", "cash")
# How to combine a percentage cap and a fixed-USD cap when BOTH are configured.
#   min  - most conservative; deploy no more than the smaller of the two (default)
#   max  - loosest; allow up to the larger of the two
#   usd  - the fixed-USD cap wins (percentage ignored)
#   pct  - the percentage cap wins (fixed-USD ignored)
_VALID_PRECEDENCE = ("min", "max", "usd", "pct")

_ZERO = Decimal("0")
_ONE = Decimal("1")


class CapitalPolicyError(ValueError):
    """Raised when a policy fails validation.

    Carries a machine-readable ``errors`` list so a future frontend can render
    field-level validation messages without parsing prose. Each entry is::

        {"field": str, "value": Any, "code": str, "msg": str}
    """

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("; ".join(e["msg"] for e in errors) or "invalid capital policy")


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Best-effort conversion to Decimal via str (avoids binary-float drift).
    Returns None for None/blank; raises InvalidOperation for un-parseable input."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    if isinstance(value, float):
        # str(float) keeps the shortest round-trippable form; good enough for money knobs.
        return Decimal(str(value))
    return Decimal(value)


@dataclass(frozen=True)
class DeployableCapitalPolicy:
    """Immutable, validated cap on total committed capital.

    Build via :meth:`from_mapping` (which validates and raises
    :class:`CapitalPolicyError`); the constructor assumes already-clean values.

    Fields
    ------
    max_pct : Decimal | None
        Fraction (0..1] of the chosen ``basis`` that may be deployed. ``0.90`` =
        90%. ``None`` means "no percentage cap" (a fixed-USD cap must then exist).
    max_usd : Decimal | None
        Absolute USD ceiling on deployed capital. ``None`` means "no USD cap".
    basis : str
        ``"equity"`` (cap is a % of total account value) or ``"cash"`` (a % of
        free quote cash). Only relevant when ``max_pct`` is set.
    precedence : str
        How to combine the two caps when BOTH are set (see ``_VALID_PRECEDENCE``).
    label : str
        Human-friendly name for logs/audit (e.g. ``"spot"``, ``"carry"``).
    """

    max_pct: Optional[Decimal]
    max_usd: Optional[Decimal]
    basis: str = "equity"
    precedence: str = "min"
    label: str = "default"

    # ------------------------------------------------------------------ #
    # Construction / validation                                          #
    # ------------------------------------------------------------------ #
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, label: str = "default") -> "DeployableCapitalPolicy":
        """Validate a raw mapping and return a policy, or raise CapitalPolicyError.

        Accepts the public field names ``max_pct`` / ``max_usd`` / ``basis`` /
        ``precedence``. Unknown keys are ignored so the schema can grow."""
        errors: list[dict[str, Any]] = []

        def _num(field: str) -> Optional[Decimal]:
            try:
                return _to_decimal(data.get(field))
            except (InvalidOperation, TypeError, ValueError):
                errors.append({"field": field, "value": data.get(field),
                               "code": "not_a_number", "msg": f"{field} must be a number"})
                return None

        max_pct = _num("max_pct")
        max_usd = _num("max_usd")
        basis = str(data.get("basis", "equity")).strip().lower()
        precedence = str(data.get("precedence", "min")).strip().lower()

        if max_pct is not None and not (_ZERO < max_pct <= _ONE):
            errors.append({"field": "max_pct", "value": float(max_pct), "code": "out_of_range",
                           "msg": "max_pct must be in the interval (0, 1] (e.g. 0.90 = 90%)"})
        if max_usd is not None and max_usd < _ZERO:
            errors.append({"field": "max_usd", "value": float(max_usd), "code": "negative",
                           "msg": "max_usd must be >= 0"})
        if max_pct is None and max_usd is None:
            errors.append({"field": "max_pct/max_usd", "value": None, "code": "unbounded",
                           "msg": "at least one of max_pct or max_usd must be set "
                                  "(an unbounded deployable-capital limit is not allowed)"})
        if basis not in _VALID_BASIS:
            errors.append({"field": "basis", "value": basis, "code": "invalid_choice",
                           "msg": f"basis must be one of {_VALID_BASIS}"})
        if precedence not in _VALID_PRECEDENCE:
            errors.append({"field": "precedence", "value": precedence, "code": "invalid_choice",
                           "msg": f"precedence must be one of {_VALID_PRECEDENCE}"})

        if errors:
            raise CapitalPolicyError(errors)
        return cls(max_pct=max_pct, max_usd=max_usd, basis=basis,
                   precedence=precedence, label=label)

    # ------------------------------------------------------------------ #
    # The actual cap calculation                                         #
    # ------------------------------------------------------------------ #
    def deployable_capital(self, equity: Any, available_cash: Any) -> Decimal:
        """The GROSS dollar envelope permitted right now (before subtracting what
        is already committed). Always >= 0."""
        eq = max(_ZERO, _to_decimal(equity) or _ZERO)
        cash = max(_ZERO, _to_decimal(available_cash) or _ZERO)

        pct_cap: Optional[Decimal] = None
        if self.max_pct is not None:
            base_amount = eq if self.basis == "equity" else cash
            pct_cap = base_amount * self.max_pct
        usd_cap = self.max_usd

        if pct_cap is not None and usd_cap is not None:
            if self.precedence == "usd":
                env = usd_cap
            elif self.precedence == "pct":
                env = pct_cap
            elif self.precedence == "max":
                env = max(pct_cap, usd_cap)
            else:  # "min" - the safe default
                env = min(pct_cap, usd_cap)
        else:
            env = pct_cap if pct_cap is not None else (usd_cap if usd_cap is not None else _ZERO)
        return max(_ZERO, env)

    def remaining_capacity(self, equity: Any, available_cash: Any, committed: Any,
                           *, clamp_to_cash: bool = False) -> Decimal:
        """Headroom for a NEW commitment: deployable - already-committed, floored
        at zero. With ``clamp_to_cash`` the result is additionally capped at
        available cash so the caller can never plan to deploy money it does not
        hold."""
        deployable = self.deployable_capital(equity, available_cash)
        committed_d = max(_ZERO, _to_decimal(committed) or _ZERO)
        remaining = deployable - committed_d
        if clamp_to_cash:
            cash = max(_ZERO, _to_decimal(available_cash) or _ZERO)
            remaining = min(remaining, cash)
        return max(_ZERO, remaining)

    # ------------------------------------------------------------------ #
    # Serialization (frontend-ready)                                     #
    # ------------------------------------------------------------------ #
    def to_public_dict(self) -> dict[str, Any]:
        """JSON-serializable view of the policy for an API/frontend. Decimals are
        rendered as floats (None stays None)."""
        return {
            "label": self.label,
            "max_pct": None if self.max_pct is None else float(self.max_pct),
            "max_usd": None if self.max_usd is None else float(self.max_usd),
            "basis": self.basis,
            "precedence": self.precedence,
        }

    def describe(self) -> str:
        """One-line human summary for startup logs."""
        parts = []
        if self.max_pct is not None:
            parts.append(f"<= {float(self.max_pct):.0%} of {self.basis}")
        if self.max_usd is not None:
            parts.append(f"<= ${float(self.max_usd):,.2f}")
        joiner = f" [{self.precedence}] " if len(parts) == 2 else " "
        return f"deployable capital {joiner.join(parts)}"


# --------------------------------------------------------------------------- #
# Schema metadata (so a frontend can render a form without hard-coding it)     #
# --------------------------------------------------------------------------- #
CAPITAL_POLICY_SCHEMA: dict[str, Any] = {
    "fields": {
        "max_pct": {"type": "number", "min": 0, "max": 1, "required": False,
                    "help": "Fraction of the basis that may be deployed (0.90 = 90%)."},
        "max_usd": {"type": "number", "min": 0, "required": False,
                    "help": "Absolute USD ceiling on deployed capital."},
        "basis": {"type": "enum", "choices": list(_VALID_BASIS), "default": "equity",
                  "help": "What max_pct is a percentage of."},
        "precedence": {"type": "enum", "choices": list(_VALID_PRECEDENCE), "default": "min",
                       "help": "How to combine max_pct and max_usd when both are set."},
    },
    "constraints": ["at least one of max_pct or max_usd must be set"],
}
