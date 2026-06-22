"""
Parameter proposals + the human approval gate (the safety core of the ops agent).

Hard guarantees (enforced here, not by convention):
  * The agent can ONLY propose changes to keys on the configured ALLOWLIST
    (`ops_agent.proposals.tunable_keys`), each with min/max bounds + type. Anything
    touching a BLOCKED prefix (safety rails, capital policy, portfolio caps) is
    rejected outright.
  * A proposal's stated `current` value must still match the live config (drift
    guard) or it is rejected.
  * Nothing is ever applied without an explicit `status: approved` set by a human,
    plus an `--approver` identity.
  * Applying edits the YAML SURGICALLY (one scalar, comments preserved), then RELOADS
    and verifies exactly that one leaf changed - otherwise it restores the backup.
  * Every write is appended to an immutable audit log.

The review artifact is a plain YAML file under `ops/proposals/`; approval is a
one-field edit (or the `approve` CLI). This keeps the gate transparent and
greppable, with no hidden state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

import yaml
from loguru import logger


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_num(v: Any, is_int: bool) -> str:
    if is_int:
        return str(int(round(float(v))))
    f = float(v)
    return repr(f) if (abs(f) >= 1e-4 or f == 0) else f"{f:.10f}".rstrip("0")


# --------------------------------------------------------------------------- #
# Proposal model + validation                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class Proposal:
    key: str                       # dotted config path, e.g. strategy.donchian.atr_trail_mult
    current: Any
    proposed: Any
    rationale: str = ""
    expected_impact: str = ""
    confidence: str = "low"        # low | medium | high
    source: str = "llm"            # llm | rule | research
    status: str = "pending"        # pending | approved | rejected | applied
    approver: str = ""
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_key(cfg: dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _is_blocked(dotted: str, blocked: list[str]) -> bool:
    return any(dotted == b or dotted.startswith(b + ".") for b in blocked)


def sanitize_allowlist(tunable: dict[str, Any], blocked: list[str],
                       cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Make the safety boundary self-enforcing: return a cleaned allowlist that
    drops any key which (a) falls under a blocked prefix, or (b) does not resolve in
    the live config. A misconfigured allowlist therefore can NEVER expose a safety
    key or a phantom key - the agent only ever sees the sanitized set. Returns
    (clean_allowlist, warnings)."""
    clean: dict[str, Any] = {}
    warns: list[str] = []
    for key, spec in (tunable or {}).items():
        if _is_blocked(key, blocked):
            warns.append(f"allowlist key '{key}' is under a blocked prefix - DROPPED (safety)")
            continue
        if resolve_key(cfg, key) is None:
            warns.append(f"allowlist key '{key}' does not resolve in config - DROPPED")
            continue
        clean[key] = spec
    return clean, warns


def validate_proposal(p: Proposal, cfg: dict[str, Any], tunable: dict[str, Any],
                      blocked: list[str]) -> tuple[bool, str]:
    """Return (ok, reason). A proposal must be on the allowlist, off the blocklist,
    in-bounds, a real change, and consistent with the live `current` value."""
    if _is_blocked(p.key, blocked):
        return False, f"'{p.key}' is on the safety blocklist - never proposable"
    spec = tunable.get(p.key)
    if spec is None:
        return False, f"'{p.key}' is not on the tunable allowlist"
    live = resolve_key(cfg, p.key)
    if live is None:
        return False, f"'{p.key}' does not resolve in the live config"
    # drift guard: the proposal's `current` must match the live value.
    try:
        if abs(float(live) - float(p.current)) > 1e-9 * max(1.0, abs(float(live))):
            return False, f"stale: live {p.key}={live} != proposal.current {p.current}"
    except (TypeError, ValueError):
        if str(live) != str(p.current):
            return False, f"stale: live {p.key}={live} != proposal.current {p.current}"
    is_int = str(spec.get("type", "")).lower() == "int"
    try:
        new = int(round(float(p.proposed))) if is_int else float(p.proposed)
    except (TypeError, ValueError):
        return False, f"proposed value {p.proposed!r} is not numeric"
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and new < float(lo):
        return False, f"{p.key} proposed {new} < min {lo}"
    if hi is not None and new > float(hi):
        return False, f"{p.key} proposed {new} > max {hi}"
    if abs(float(new) - float(live)) <= 1e-12:
        return False, f"{p.key} proposed value equals current ({live}) - no-op"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Surgical, comment-preserving YAML scalar edit                               #
# --------------------------------------------------------------------------- #
def set_yaml_scalar(text: str, dotted: str, new_repr: str) -> tuple[str, str]:
    """Replace the scalar at `dotted` (path-aware) with `new_repr`, preserving
    indentation and any inline comment. Returns (new_text, old_value_str). Raises
    KeyError if the path's leaf line can't be found."""
    parts = dotted.split(".")
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []        # (indent, key) ancestor chain
    key_re = re.compile(r"^(\s*)([A-Za-z0-9_]+)\s*:(.*)$")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = key_re.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key, rest = m.group(2), m.group(3)
        path = [k for _, k in stack] + [key]
        if path == parts:
            mval = re.match(r"^(\s*)(\S+)(.*)$", rest)
            if not mval:
                raise KeyError(f"{dotted}: leaf has no scalar value to replace")
            lead, old_val, tail = mval.groups()
            lines[i] = f"{m.group(1)}{key}:{lead}{new_repr}{tail}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), old_val
        stack.append((indent, key))
    raise KeyError(f"{dotted}: path not found in YAML")


def _diff_paths(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose leaf values differ between two loaded configs."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            out += _diff_paths(a.get(k), b.get(k), f"{prefix}{k}.")
    elif a != b:
        out.append(prefix.rstrip("."))
    return out


# --------------------------------------------------------------------------- #
# Approval gate (file-based)                                                   #
# --------------------------------------------------------------------------- #
class ApprovalGate:
    def __init__(self, proposals_dir: str = "ops/proposals", audit_log: str = "ops/audit.log"):
        self.dir = proposals_dir
        self.audit_log = audit_log

    # ---- write / list ---------------------------------------------------- #
    def write_pending(self, proposals: list[Proposal], context: dict[str, Any],
                      mode: str) -> str:
        os.makedirs(self.dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(self.dir, f"{stamp}_{mode}_review.yaml")
        doc = {
            "created_at": _utcnow(), "mode": mode, "status": "pending",
            "context": context,
            "proposals": [p.to_dict() for p in proposals],
            "approval": {"how": "set a proposal's status to 'approved' (or run "
                         "`--mode approve --file <this> --approver YOU`), then "
                         "`--mode apply --file <this> --approver YOU`."},
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
        self._audit("write_pending", {"path": path, "mode": mode, "n": len(proposals)})
        logger.info("Ops: wrote {} proposal(s) to {} (status pending).", len(proposals), path)
        return path

    def list_pending(self) -> list[tuple[str, dict[str, Any]]]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith("_review.yaml"):
                p = os.path.join(self.dir, fn)
                try:
                    with open(p, encoding="utf-8") as fh:
                        out.append((p, yaml.safe_load(fh)))
                except Exception as exc:
                    logger.warning("Could not read {}: {}", p, exc)
        return out

    def approve(self, path: str, approver: str, indices: Optional[list[int]] = None) -> int:
        """Mark proposals approved (all, or just `indices`). Returns count approved."""
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        n = 0
        for i, pr in enumerate(doc.get("proposals", [])):
            if indices is not None and i not in indices:
                continue
            if pr.get("status") == "pending":
                pr["status"] = "approved"; pr["approver"] = approver; pr["decided_at"] = _utcnow()
                n += 1
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
        self._audit("approve", {"path": path, "approver": approver, "approved": n})
        return n

    # ---- apply (the only writer to the live config) ---------------------- #
    def apply_approved(self, path: str, config_path: str, cfg: dict[str, Any],
                       tunable: dict[str, Any], blocked: list[str], approver: str) -> dict[str, Any]:
        """Apply every APPROVED + VALID proposal in `path` to the live YAML, each as a
        verified surgical edit with a backup. Returns {applied, skipped}."""
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for pr in doc.get("proposals", []):
            if pr.get("status") != "approved":
                if pr.get("status") == "pending":
                    skipped.append({"key": pr.get("key"), "why": "not approved"})
                continue
            prop = Proposal(**{k: pr[k] for k in pr if k in Proposal.__annotations__})
            ok, why = validate_proposal(prop, cfg, tunable, blocked)
            if not ok:
                skipped.append({"key": prop.key, "why": why})
                logger.warning("Ops apply SKIP {}: {}", prop.key, why)
                continue
            try:
                old = self._apply_one(config_path, prop, tunable)
            except Exception as exc:
                skipped.append({"key": prop.key, "why": f"edit failed: {exc}"})
                logger.error("Ops apply FAILED {}: {}", prop.key, exc)
                continue
            pr["status"] = "applied"; pr["approver"] = approver; pr["decided_at"] = _utcnow()
            # keep the in-memory cfg in sync so multiple edits in one run drift-check OK
            _set_in_dict(cfg, prop.key, _coerce(prop.proposed, tunable[prop.key]))
            applied.append({"key": prop.key, "from": old, "to": prop.proposed})
            self._audit("apply", {"config": config_path, "key": prop.key, "from": old,
                                  "to": prop.proposed, "approver": approver, "review": path})
            logger.warning("Ops APPLIED {}: {} -> {} (approver {}).",
                           prop.key, old, prop.proposed, approver)
        if applied:
            doc["status"] = "applied"
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
        return {"applied": applied, "skipped": skipped}

    def _apply_one(self, config_path: str, prop: Proposal, tunable: dict[str, Any]) -> str:
        """Backup -> surgical edit -> reload + verify exactly-one-leaf-changed. Restores
        the backup and raises on any verification failure. Returns the old value str."""
        with open(config_path, encoding="utf-8") as fh:
            before_text = fh.read()
        before_cfg = yaml.safe_load(before_text)
        is_int = str(tunable[prop.key].get("type", "")).lower() == "int"
        new_repr = _fmt_num(prop.proposed, is_int)
        new_text, old_val = set_yaml_scalar(before_text, prop.key, new_repr)

        backup = f"{config_path}.bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(config_path, backup)
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        try:
            after_cfg = yaml.safe_load(new_text)
            changed = _diff_paths(before_cfg, after_cfg)
            want = prop.key
            new_live = resolve_key(after_cfg, want)
            expect = int(round(float(prop.proposed))) if is_int else float(prop.proposed)
            if changed != [want] or abs(float(new_live) - float(expect)) > 1e-9:
                raise ValueError(f"verification failed: changed={changed}, leaf={new_live}")
        except Exception:
            shutil.copy2(backup, config_path)   # restore
            raise
        return old_val

    # ---- audit ----------------------------------------------------------- #
    def _audit(self, action: str, payload: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.audit_log) or ".", exist_ok=True)
        rec = {"ts": _utcnow(), "action": action, **payload}
        with open(self.audit_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")


def _coerce(v: Any, spec: dict[str, Any]) -> Any:
    return int(round(float(v))) if str(spec.get("type", "")).lower() == "int" else float(v)


def _set_in_dict(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    cur = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
