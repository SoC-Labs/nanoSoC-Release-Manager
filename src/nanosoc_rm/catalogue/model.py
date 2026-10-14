"""The catalogue characterisation model (interfaces.md section 23.1; CR-WF-catalogue).

    characterise(component) -> Characterisation             one catalogue component, defaults derived from class
    characterise_all(components) -> {id: Characterisation}  every component, with `wrapped_by` filled in
    socs_without_forkable_project(chars) -> [id]            the gap the front page flags
    effective_group(component) -> str                       the explicit group, else the class default

Pure: no I/O, no registry import (the loader's rule 15 imports this module).

A catalogue written before CR-WF-catalogue has no `group`, `type`, `role`, `wraps`, `guessed` or `question`.
Such an entry still loads: its group comes from `class` (DEFAULT_GROUP), its role from `class` where the class
says one (DEFAULT_ROLE), its type stays None, and every field filled this way is listed in `derived`, so a page
shows it as a default to confirm, never as a decision. `unconfirmed` is `guessed` plus `derived`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

__all__ = ["GROUPS", "LADDER_GROUPS", "TYPES", "ROLES", "GUESSABLE", "DEFAULT_GROUP", "DEFAULT_ROLE",
           "CLASS_FOR_GROUP", "WRAP_TARGETS", "GROUP_LABELS", "TYPE_LABELS", "ROLE_LABELS", "Characterisation",
           "characterise", "characterise_all", "effective_group", "socs_without_forkable_project", "class_for"]

GROUPS = ("project", "soc", "subsystem", "component", "toolkit", "flow", "generator", "arch", "lib", "infra",
          "legacy")
LADDER_GROUPS = ("project", "soc", "subsystem")          # the groups that carry `type` and `role`
TYPES = ("chip", "chiplet", "soc", "subsystem")
ROLES = ("implementation", "forkable", "example")
GUESSABLE = ("group", "type", "role", "wraps", "inside")
# What a group may wrap (loader rule 15): a project its SoC (or a subsystem it integrates directly); a SoC its
# subsystems and the components it integrates.
WRAP_TARGETS = {"project": ("soc", "subsystem"), "soc": ("subsystem", "component")}

# Today's B1 class -> the default group and role, for entries that do not state them.
DEFAULT_GROUP = {"soc_ref": "project", "example": "project", "generator": "generator", "arch_tech": "arch",
                 "ip_tech": "component", "toolkit": "toolkit", "infra": "infra", "legacy": "legacy"}
DEFAULT_ROLE = {"soc_ref": "implementation", "example": "example"}
# The B1 class a NEW entry gets from its group (catalogue preview, section 23.3); role `example` makes a ladder
# entry class `example`.
CLASS_FOR_GROUP = {"project": "soc_ref", "soc": "soc_ref", "subsystem": "ip_tech", "component": "ip_tech",
                   "toolkit": "toolkit", "flow": "toolkit", "generator": "generator", "arch": "arch_tech",
                   "lib": "ip_tech", "infra": "infra", "legacy": "legacy"}

GROUP_LABELS = {"project": "Project", "soc": "SoC", "subsystem": "Subsystem", "component": "Component (tech)",
                "toolkit": "Toolkit", "flow": "Flow", "generator": "Generator", "arch": "Architecture tech unit",
                "lib": "Technology library", "infra": "Board / infra", "legacy": "Legacy"}
TYPE_LABELS = {"chip": "Chip", "chiplet": "Chiplet", "soc": "SoC", "subsystem": "Subsystem"}
ROLE_LABELS = {"implementation": "Implementation", "forkable": "Forkable template", "example": "Example"}


@dataclass(frozen=True)
class Characterisation:
    """One component as the front page groups it.

    id, cls          the catalogue id and its B1 class
    group            one of GROUPS (explicit, or derived from class)
    type, role       ladder groups only (else None); type None also when derived and unknown
    wraps            ids this one integrates (project/soc only), catalogue order
    wrapped_by       ids of the entries whose `wraps` names this one (characterise_all only)
    guessed          fields the catalogue marks as a judgement to confirm
    derived          fields the catalogue does not state, filled from class (or unknown: type)
    question         the open question to the registry owner, or None
    top_level        True for group project: the plate that wraps a SoC
    """
    id: str
    cls: str
    group: str
    type: str | None = None
    role: str | None = None
    wraps: tuple[str, ...] = ()
    wrapped_by: tuple[str, ...] = ()
    guessed: tuple[str, ...] = ()
    derived: tuple[str, ...] = ()
    question: str | None = None
    top_level: bool = False

    @property
    def unconfirmed(self) -> tuple[str, ...]:
        return tuple(f for f in GUESSABLE if f in self.guessed or f in self.derived)

    def as_doc(self) -> dict[str, Any]:
        return {"id": self.id, "class": self.cls, "group": self.group, "type": self.type, "role": self.role,
                "wraps": list(self.wraps), "wrapped_by": list(self.wrapped_by), "guessed": list(self.guessed),
                "derived": list(self.derived), "unconfirmed": list(self.unconfirmed), "question": self.question,
                "top_level": self.top_level}


def effective_group(component: dict) -> str | None:
    """The component's `group`, else its class default; None when neither is known (a malformed entry)."""
    g = component.get("group")
    if isinstance(g, str) and g in GROUPS:
        return g
    return DEFAULT_GROUP.get(component.get("class"))  # type: ignore[arg-type]


def class_for(group: str, role: str | None = None) -> str:
    """The B1 class a new entry of `group` (and `role`) gets."""
    if group in LADDER_GROUPS and role == "example":
        return "example"
    return CLASS_FOR_GROUP[group]


def characterise(component: dict) -> Characterisation:
    """One schema-valid catalogue component as a Characterisation (wrapped_by empty)."""
    cls = component["class"]
    derived: list[str] = []
    group = component.get("group")
    if group is None:
        group = DEFAULT_GROUP[cls]
        derived.append("group")
    typ, role = component.get("type"), component.get("role")
    if group in LADDER_GROUPS:
        if typ is None:
            typ = {"soc": "soc", "subsystem": "subsystem"}.get(group)
            derived.append("type")
        if role is None:
            role = DEFAULT_ROLE.get(cls, "implementation")
            derived.append("role")
    else:
        typ = role = None
    return Characterisation(
        id=component["id"], cls=cls, group=group, type=typ, role=role,
        wraps=tuple(component.get("wraps") or ()), guessed=tuple(component.get("guessed") or ()),
        derived=tuple(derived), question=component.get("question"), top_level=group == "project")


def characterise_all(components: Iterable[dict]) -> dict[str, Characterisation]:
    """{id: Characterisation} for every component, in catalogue order, with `wrapped_by` filled in."""
    chars = {c["id"]: characterise(c) for c in components}
    parents: dict[str, list[str]] = {}
    for ch in chars.values():
        for w in ch.wraps:
            parents.setdefault(w, []).append(ch.id)
    out = {}
    for cid, ch in chars.items():
        out[cid] = replace(ch, wrapped_by=tuple(parents.get(cid, ())))
    return out


def socs_without_forkable_project(chars: dict[str, Characterisation]) -> list[str]:
    """Group-soc entries that no project with role `forkable` wraps (David 2026-10-14: the gap filter and badge).
    Wrapped by an implementation or an example only, or by nothing, is a gap."""
    return [cid for cid, ch in chars.items()
            if ch.group == "soc"
            and not any(chars[p].group == "project" and chars[p].role == "forkable"
                        for p in ch.wrapped_by if p in chars)]
