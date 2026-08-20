"""Attribute-level preferences: Beta-smoothed (alpha=positive, beta=negative) per tag/source/author/entity,
plus static profile.yaml priors. Multiplier ∈ [0.5, 1.5] per attribute; product clipped to [0.25, 2.0]; muted → 0."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Preference
from ..settings import get_profile

POS_ACTIONS = {"save": 1.0, "dig": 1.0, "click": 0.3, "expand": 0.3, "read": 0.2}
NEG_ACTIONS = {"ignore": 1.0, "mute_source": 2.0}


def _mean_to_mult(mean: float) -> float:
    return 0.5 + mean  # mean 0.5 → 1.0 (neutral)


class PrefModel:
    def __init__(self, prefs: dict[tuple[str, str], Preference], profile: dict, now: datetime):
        self.prefs = prefs
        self.profile = profile or {}
        self.now = now

    def attr_mult(self, kind: str, key: str) -> tuple[float, bool]:
        """Returns (multiplier, muted)."""
        p = self.prefs.get((kind, key))
        if p and p.muted_until and p.muted_until > self.now:
            return 0.0, True
        m = 1.0
        if p:
            m = _mean_to_mult(p.alpha / (p.alpha + p.beta))
        return m, False

    def profile_mult(self, group: str, key: str) -> float:
        return float((self.profile.get(group) or {}).get(key, 1.0))

    def item_multiplier(self, item: Item) -> tuple[float, dict]:
        detail: dict[str, float] = {}
        mult = 1.0
        muted = False
        for s in item.sources:
            skey = s.source.split(":")[0]
            m, mu = self.attr_mult("source", s.source)
            m2, mu2 = self.attr_mult("source", skey) if skey != s.source else (1.0, False)
            muted |= mu or mu2
            detail[f"source:{s.source}"] = m * m2
            mult *= m * m2
            if s.author_key:
                m, mu = self.attr_mult("author", f"{skey}:{s.author_key}")
                muted |= mu
                if m != 1.0:
                    detail[f"author:{s.author_key}"] = m
                mult *= m
        tags = item.tags or {}
        for group in ("domains", "stacks"):
            for t in tags.get(group) or []:
                m, mu = self.attr_mult("tag", t)
                muted |= mu
                pm = self.profile_mult(group, t)
                if m * pm != 1.0:
                    detail[f"tag:{t}"] = m * pm
                mult *= m * pm
        if tags.get("type"):
            pm = self.profile_mult("types", tags["type"])
            m, mu = self.attr_mult("tag", tags["type"])
            muted |= mu
            mult *= m * pm
            if m * pm != 1.0:
                detail[f"type:{tags['type']}"] = m * pm
        # tags_hint (pre-enrich): map github language/topics onto profile.stacks
        if not tags:
            for s in item.sources:
                for h in (s.raw or {}).get("tags_hint") or []:
                    pm = self.profile_mult("stacks", h)
                    if pm != 1.0:
                        detail[f"hint:{h}"] = pm
                        mult *= pm
        for e in item.entities_matched or []:
            m, mu = self.attr_mult("entity", e)
            muted |= mu
            if m != 1.0:
                detail[f"entity:{e}"] = m
            mult *= m
        if muted:
            return 0.0, {**detail, "muted": 1.0}
        return max(0.25, min(2.0, mult)), detail


def load_pref_model(session: Session, now: datetime | None = None) -> PrefModel:
    now = now or datetime.now(timezone.utc)
    prefs = {(p.kind, p.key): p for p in session.scalars(select(Preference)).all()}
    return PrefModel(prefs, get_profile(), now)


def apply_feedback_to_prefs(session: Session, item: Item, action: str) -> None:
    """Update Beta counts for the item's attributes. Called by feedback service."""
    w_pos = POS_ACTIONS.get(action, 0.0)
    w_neg = NEG_ACTIONS.get(action, 0.0)
    if not (w_pos or w_neg):
        return
    keys: list[tuple[str, str]] = []
    for s in item.sources:
        keys.append(("source", s.source))
        if s.author_key:
            keys.append(("author", f"{s.source.split(':')[0]}:{s.author_key}"))
    tags = item.tags or {}
    for group in ("domains", "stacks"):
        keys += [("tag", t) for t in tags.get(group) or []]
    if tags.get("type"):
        keys.append(("tag", tags["type"]))
    keys += [("entity", e) for e in item.entities_matched or []]
    for kind, key in set(keys):
        p = session.get(Preference, (kind, key))
        if p is None:
            p = Preference(kind=kind, key=key, alpha=1.0, beta=1.0)
            session.add(p)
        p.alpha += w_pos
        p.beta += w_neg
