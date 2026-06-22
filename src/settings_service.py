"""
Capital-settings service - the user-facing, frontend-ready surface for the
deployable-capital limit.

Responsibilities (and ONLY these - it owns no trading logic):
  1. Resolve the effective :class:`DeployableCapitalPolicy` for a sleeve
     ("spot" / "carry" / "etf") from a clear precedence chain.
  2. Persist user changes to a JSON override file so they survive restarts and
     can be reloaded at runtime, with atomic writes.
  3. Append an audit-trail line for every change (who, when, before, after).
  4. Return structured, machine-readable results that a REST/GraphQL layer can
     hand straight to a frontend.

Resolution precedence for a sleeve (highest wins):
  1. Environment variables      (operator "flip-it-fast"; spot sleeve only)
  2. Persisted JSON override    (what the settings service / frontend wrote)
  3. YAML ``capital_policy``     block in config/trading_config.yaml
  4. Legacy strategy config      (portfolio.max_total_exposure_pct, sleeve_usd...)

Each lower layer supplies defaults that the higher layers merge over, so a user
who sets only ``MAX_DEPLOYED_CAPITAL_USD`` still keeps the YAML percentage cap
(the two then combine via ``precedence``, default ``min`` = most conservative).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from loguru import logger

from src.capital_policy import CapitalPolicyError, DeployableCapitalPolicy

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OVERRIDE_PATH = _REPO_ROOT / "config" / "capital_limits.json"
_DEFAULT_AUDIT_PATH = _REPO_ROOT / "config" / "capital_limits_audit.log"

SLEEVES = ("spot", "carry", "etf")

# Per-sleeve environment overrides. Only the spot sleeve gets the new generic
# vars; carry/etf already flip their sleeve via CARRY_SLEEVE_USD / ETF_SLEEVE_USD,
# which feed the legacy default below.
_SPOT_ENV = {
    "max_usd": "MAX_DEPLOYED_CAPITAL_USD",
    "max_pct": "MAX_DEPLOYED_CAPITAL_PCT",
    "basis": "MAX_DEPLOYED_CAPITAL_BASIS",
    "precedence": "MAX_DEPLOYED_CAPITAL_PRECEDENCE",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_mapping(cfg: Mapping[str, Any], sleeve: str) -> dict[str, Any]:
    """The pre-refactor implicit limit for a sleeve, expressed as a policy mapping.
    This is what keeps behavior identical when no new config is supplied."""
    if sleeve == "spot":
        pf = cfg.get("portfolio", {}) or {}
        return {"max_pct": pf.get("max_total_exposure_pct", 0.90),
                "max_usd": None, "basis": "equity", "precedence": "min"}
    if sleeve == "carry":
        cap = (cfg.get("carry", {}) or {}).get("capital", {}) or {}
        return {"max_pct": None, "max_usd": cap.get("sleeve_usd"),
                "basis": "equity", "precedence": "min"}
    if sleeve == "etf":
        # The ETF sleeve_usd seeds paper cash; the *envelope* cap was historically
        # equity * max_total_exposure_pct only. Keep that as the legacy default so
        # behavior is identical until the user opts into a USD cap.
        cap = (cfg.get("etf", {}) or {}).get("capital", {}) or {}
        return {"max_pct": cap.get("max_total_exposure_pct", 0.95), "max_usd": None,
                "basis": "equity", "precedence": "min"}
    return {}


def _env_overrides(sleeve: str) -> dict[str, Any]:
    if sleeve != "spot":
        return {}
    out: dict[str, Any] = {}
    for field, env_name in _SPOT_ENV.items():
        raw = os.getenv(env_name)
        if raw is not None and raw.strip() != "":
            out[field] = raw.strip()
    return out


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay ``over`` onto ``base``, ignoring None values in ``over`` so a
    partial override never blanks a configured cap."""
    out = dict(base)
    for k, v in over.items():
        if v is not None:
            out[k] = v
    return out


class CapitalSettingsService:
    """Load / read / update the deployable-capital policy for each sleeve."""

    def __init__(self, cfg: Mapping[str, Any], *,
                 override_path: Optional[Path] = None,
                 audit_path: Optional[Path] = None):
        self.cfg = cfg
        self.override_path = Path(os.getenv("CAPITAL_LIMITS_PATH", str(
            override_path or _DEFAULT_OVERRIDE_PATH)))
        self.audit_path = Path(audit_path or _DEFAULT_AUDIT_PATH)
        self._override_mtime: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Persistence (JSON override file)                                   #
    # ------------------------------------------------------------------ #
    def _read_override_file(self) -> dict[str, Any]:
        if not self.override_path.exists():
            return {}
        try:
            with open(self.override_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Capital override file unreadable ({}); ignoring it.", exc)
            return {}

    def _write_override_file(self, data: Mapping[str, Any]) -> None:
        """Atomic write (temp file + os.replace) so a crash never leaves a
        half-written limits file that could mis-state the cap on restart."""
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.override_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.override_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._override_mtime = self.override_path.stat().st_mtime

    # ------------------------------------------------------------------ #
    # Resolution                                                         #
    # ------------------------------------------------------------------ #
    def resolve_mapping(self, sleeve: str = "spot") -> tuple[dict[str, Any], str]:
        """Return (effective_policy_mapping, winning_source) for a sleeve."""
        mapping = _legacy_mapping(self.cfg, sleeve)
        source = "legacy"

        yaml_block = self.cfg.get("capital_policy") or {}
        # Support both a per-sleeve block (capital_policy.spot: {...}) and, for the
        # spot sleeve, a flat block (capital_policy: {max_pct: ...}).
        yaml_for_sleeve: Optional[Mapping[str, Any]] = None
        if isinstance(yaml_block.get(sleeve), Mapping):
            yaml_for_sleeve = yaml_block[sleeve]
        elif sleeve == "spot" and ("max_pct" in yaml_block or "max_usd" in yaml_block):
            yaml_for_sleeve = yaml_block
        if yaml_for_sleeve:
            mapping = _merge(mapping, yaml_for_sleeve)
            source = "yaml"

        override = self._read_override_file().get(sleeve)
        if isinstance(override, Mapping):
            mapping = _merge(mapping, override)
            source = "override_file"

        env = _env_overrides(sleeve)
        if env:
            mapping = _merge(mapping, env)
            source = "env"

        return mapping, source

    def policy(self, sleeve: str = "spot") -> DeployableCapitalPolicy:
        """Build the validated, effective policy for a sleeve.
        Raises CapitalPolicyError if the resolved mapping is invalid."""
        mapping, _src = self.resolve_mapping(sleeve)
        return DeployableCapitalPolicy.from_mapping(mapping, label=sleeve)

    def override_changed_on_disk(self) -> bool:
        """Cheap mtime check so a long-running loop can hot-reload the policy
        only when the override file actually changed."""
        try:
            mtime = self.override_path.stat().st_mtime
        except OSError:
            mtime = None
        changed = mtime != self._override_mtime
        self._override_mtime = mtime
        return changed

    # ------------------------------------------------------------------ #
    # Read API (frontend-ready)                                          #
    # ------------------------------------------------------------------ #
    def get(self, sleeve: str = "spot") -> dict[str, Any]:
        """Structured current-state payload for a GET endpoint."""
        mapping, source = self.resolve_mapping(sleeve)
        try:
            pol = DeployableCapitalPolicy.from_mapping(mapping, label=sleeve)
            return {"ok": True, "sleeve": sleeve, "source": source,
                    "policy": pol.to_public_dict(), "description": pol.describe()}
        except CapitalPolicyError as exc:
            return {"ok": False, "sleeve": sleeve, "source": source, "errors": exc.errors}

    def get_all(self) -> dict[str, Any]:
        return {sleeve: self.get(sleeve) for sleeve in SLEEVES}

    # ------------------------------------------------------------------ #
    # Write API (validate -> persist -> audit)                           #
    # ------------------------------------------------------------------ #
    def update(self, payload: Mapping[str, Any], *, sleeve: str = "spot",
               actor: str = "api") -> dict[str, Any]:
        """Validate and persist a new policy for a sleeve.

        Returns a structured result. On validation failure NOTHING is written and
        the result carries machine-readable ``errors``. Note: this writes to the
        override file (precedence layer 2); if an environment override is shadowing
        the sleeve, the saved value will not take effect until that env var is
        cleared - the result flags this via ``shadowed_by_env``."""
        # Start from the current effective mapping so a partial update only
        # changes the fields the caller supplied.
        current, _src = self.resolve_mapping(sleeve)
        candidate = _merge(current, {k: payload.get(k) for k in
                                     ("max_pct", "max_usd", "basis", "precedence")
                                     if k in payload})
        # Allow explicit clearing of a cap by passing it as null.
        for k in ("max_pct", "max_usd"):
            if k in payload and payload[k] is None:
                candidate[k] = None

        try:
            pol = DeployableCapitalPolicy.from_mapping(candidate, label=sleeve)
        except CapitalPolicyError as exc:
            logger.warning("Rejected capital-limit update for {} by {}: {}",
                           sleeve, actor, exc)
            return {"ok": False, "sleeve": sleeve, "errors": exc.errors}

        before = self.get(sleeve)
        store = self._read_override_file()
        store[sleeve] = {k: candidate.get(k) for k in ("max_pct", "max_usd", "basis", "precedence")}
        self._write_override_file(store)
        self._audit(sleeve, actor, before.get("policy"), pol.to_public_dict())

        shadowed = bool(_env_overrides(sleeve))
        if shadowed:
            logger.warning("Capital limit for {} saved, but an env override is shadowing it "
                           "(clear {} to use the saved value).", sleeve, list(_SPOT_ENV.values()))
        # Re-resolve so the returned state reflects precedence (incl. env shadow).
        result = self.get(sleeve)
        result["saved"] = pol.to_public_dict()
        result["shadowed_by_env"] = shadowed
        logger.info("Capital limit for {} updated by {} -> {}", sleeve, actor, pol.describe())
        return result

    def _audit(self, sleeve: str, actor: str, before: Any, after: Any) -> None:
        line = json.dumps({"ts": _utcnow_iso(), "sleeve": sleeve, "actor": actor,
                           "before": before, "after": after}, sort_keys=True)
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # pragma: no cover - never let auditing break a trade config
            logger.warning("Could not write capital-limit audit line: {}", exc)
