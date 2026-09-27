"""Generic Van Gogh Home schema discovery and C-grid migration.

This module contains no site address, customer identity, activation name, or
fixed dashboard path. It recognises only supported Van Gogh product/schema
markers and preserves all unowned dashboard configuration byte-for-byte at the
Python object level.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class MigrationError(RuntimeError):
    """The dashboard layout is unsupported or ambiguous; callers must not write."""


LEGACY_BAY_TYPE = "custom:van-gogh-c-bay-card"
GRID_TYPE = "custom:van-gogh2-home-c-grid-card"
MODULE_TYPE = "custom:van-gogh2-home-module-card"
HOME_VIEW_TYPE = "custom:van-gogh-wall10-view"
PRODUCT_MARKERS = (
    "custom:van-gogh2-home-header-card",
    "custom:van-gogh2-home-climate-card",
    "custom:van-gogh2-quick-actions-card",
    "custom:van-gogh2-navigation-card",
)
HOME_SCHEMA_CARD_TYPES = (PRODUCT_MARKERS[1], LEGACY_BAY_TYPE, GRID_TYPE)
LEGACY_BAY_STARTS = {"C1-C2": 1, "C3-C4": 3, "C5-C6": 5, "C7-C8": 7}
EXPECTED_LEGACY_ORDER = tuple(LEGACY_BAY_STARTS)
SLOTS = tuple(f"C{index}" for index in range(1, 9))
MODULE_FOOTPRINTS = {
    "security": {1, 2},
    "lighting": {2},
    "media": {1, 2},
    "rooms": {2},
    "windows": {2},
    "irrigation": {1, 2},
    "power": {1, 2},
}


@dataclass(frozen=True)
class DashboardPlan:
    url_path: str
    registry: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    state: str
    home_view_index: int

    @property
    def changed(self) -> bool:
        return self.state == "upgrade"


def _schema_one(card: dict[str, Any]) -> bool:
    return card.get("schema_version") == 1


def _validate_grid(grid: dict[str, Any]) -> dict[str, Any]:
    if grid.get("type") != GRID_TYPE or not _schema_one(grid):
        raise MigrationError("Unsupported Van Gogh Home C-grid schema")
    children = grid.get("cards")
    if not isinstance(children, list):
        raise MigrationError("Van Gogh Home C-grid cards must be an array")
    occupied: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for source in children:
        if not isinstance(source, dict):
            raise MigrationError("Van Gogh Home module must be an object")
        child = deepcopy(source)
        if child.get("type") != MODULE_TYPE or not _schema_one(child):
            raise MigrationError("Unsupported Van Gogh Home module schema")
        kind = child.get("kind")
        span = child.get("span")
        slot = child.get("slot")
        if kind not in MODULE_FOOTPRINTS:
            raise MigrationError(f"Unsupported Van Gogh Home module kind: {kind}")
        if not isinstance(span, int) or isinstance(span, bool) or span not in {1, 2}:
            raise MigrationError("Van Gogh Home module span must be Single (1) or Double (2)")
        if span not in MODULE_FOOTPRINTS[kind] and not (
            kind == "windows" and span == 1 and child.get("legacy_footprint") is True
        ):
            raise MigrationError(f"Unsupported {kind} footprint")
        if slot not in SLOTS:
            raise MigrationError("Van Gogh Home cell must be C1-C8")
        start = SLOTS.index(slot)
        if span == 2 and start in {3, 7}:
            raise MigrationError("A Double Home module must remain on the same horizontal row")
        for current in SLOTS[start:start + span]:
            if current in occupied:
                raise MigrationError(f"{current} is already occupied by {occupied[current]}")
            occupied[current] = slot
        normalized.append(child)
    normalized.sort(key=lambda card: SLOTS.index(card["slot"]))
    result = deepcopy(grid)
    result.update({"type": GRID_TYPE, "schema_version": 1, "cards": normalized})
    return result


def migrate_legacy_bays(bays: list[dict[str, Any]]) -> dict[str, Any]:
    """Port of the accepted staging.8 ``migrateLegacyHomeBays`` contract."""
    if not isinstance(bays, list):
        raise MigrationError("Legacy Home bays must be an array")
    seen: set[str] = set()
    migrated: list[dict[str, Any]] = []
    for bay in bays:
        if not isinstance(bay, dict):
            raise MigrationError("Legacy Home bay must be an object")
        name_value = bay.get("bay")
        name = name_value if isinstance(name_value, str) else ""
        start = LEGACY_BAY_STARTS.get(name)
        if start is None:
            raise MigrationError(f"Unknown legacy Home bay: {name or 'missing'}")
        if name in seen:
            raise MigrationError(f"Duplicate legacy Home bay {name}")
        seen.add(name)
        if bay.get("type") != LEGACY_BAY_TYPE or not _schema_one(bay):
            raise MigrationError(f"Unsupported legacy Home bay schema {name}")
        children_value = bay.get("cards")
        children: list[Any] = children_value if isinstance(children_value, list) else []
        mode = bay.get("mode")
        malformed = (
            (mode == "empty" and len(children) != 0)
            or (mode == "large" and len(children) != 1)
            or (mode == "split" and len(children) > 2)
            or mode not in {"empty", "large", "split"}
        )
        if malformed:
            raise MigrationError(f"Malformed legacy Home bay {name}")
        for index, source in enumerate(children):
            if not isinstance(source, dict):
                raise MigrationError(f"Malformed legacy Home module in {name}")
            child = deepcopy(source)
            slot = f"C{start + index}"
            span = 2 if mode == "large" else 1
            if child.get("slot") not in {None, slot}:
                raise MigrationError(f"Malformed legacy slot {child.get('slot')}; expected {slot}")
            if child.get("type") not in {None, MODULE_TYPE}:
                raise MigrationError(f"Malformed legacy Home module type at {slot}")
            child.update({"type": MODULE_TYPE, "schema_version": 1, "slot": slot, "span": span})
            if child.get("kind") == "windows" and span == 1:
                child["legacy_footprint"] = True
            migrated.append(child)
    if tuple(bay.get("bay") for bay in bays) != EXPECTED_LEGACY_ORDER:
        raise MigrationError("Legacy Home bays are not the recognised ordered C1-C8 schema")
    return _validate_grid({"type": GRID_TYPE, "schema_version": 1, "cards": migrated})


def _marked_home_view(view: Any) -> bool:
    if not isinstance(view, dict) or view.get("type") != HOME_VIEW_TYPE:
        return False
    cards = view.get("cards")
    if not isinstance(cards, list):
        return False
    for marker in PRODUCT_MARKERS:
        matches = [card for card in cards if isinstance(card, dict) and card.get("type") == marker]
        if len(matches) != 1 or not _schema_one(matches[0]):
            return False
    return True


def _product_schema_view(view: Any) -> bool:
    if not isinstance(view, dict) or view.get("type") != HOME_VIEW_TYPE:
        return False
    cards = view.get("cards")
    return isinstance(cards, list) and any(
        isinstance(card, dict) and card.get("type") in HOME_SCHEMA_CARD_TYPES
        for card in cards
    )


def plan_config(config: dict[str, Any]) -> tuple[str, dict[str, Any], int] | None:
    """Return an upgrade/current plan for one structurally recognised product config."""
    if not isinstance(config, dict) or not isinstance(config.get("views"), list):
        return None
    product_indices = [index for index, view in enumerate(config["views"]) if _product_schema_view(view)]
    indices = [index for index, view in enumerate(config["views"]) if _marked_home_view(view)]
    if product_indices and product_indices != indices:
        raise MigrationError("Van Gogh Home product markers are incomplete or ambiguous")
    if not indices:
        return None
    if len(indices) != 1:
        raise MigrationError("A Van Gogh dashboard has ambiguous marked Home views")
    view_index = indices[0]
    cards = config["views"][view_index]["cards"]
    legacy_indices = [index for index, card in enumerate(cards) if isinstance(card, dict) and card.get("type") == LEGACY_BAY_TYPE]
    grid_indices = [index for index, card in enumerate(cards) if isinstance(card, dict) and card.get("type") == GRID_TYPE]
    if legacy_indices and grid_indices:
        raise MigrationError("Van Gogh Home contains both legacy bays and a C-grid")
    if len(grid_indices) == 1 and not legacy_indices:
        _validate_grid(cards[grid_indices[0]])
        return "current", deepcopy(config), view_index
    if len(legacy_indices) != 4 or grid_indices:
        raise MigrationError("Van Gogh Home is not a supported four-bay or one-grid schema")
    if legacy_indices != list(range(legacy_indices[0], legacy_indices[0] + 4)):
        raise MigrationError("Legacy Van Gogh Home bays are not contiguous")
    bays = [cards[index] for index in legacy_indices]
    grid = migrate_legacy_bays(bays)
    after = deepcopy(config)
    target_cards = after["views"][view_index]["cards"]
    target_cards[legacy_indices[0]:legacy_indices[-1] + 1] = [grid]
    return "upgrade", after, view_index


def discover_dashboard(inventory: list[dict[str, Any]]) -> DashboardPlan:
    """Discover exactly one supported Installer-managed Van Gogh dashboard."""
    matches: list[DashboardPlan] = []
    failures: list[str] = []
    for snapshot in inventory:
        registry = snapshot.get("registry") if isinstance(snapshot, dict) else None
        config = snapshot.get("config") if isinstance(snapshot, dict) else None
        url_path = str((registry or {}).get("url_path") or "")
        if not isinstance(registry, dict) or registry.get("mode") != "storage" or not isinstance(config, dict):
            continue
        try:
            planned = plan_config(config)
        except MigrationError as error:
            if any(
                isinstance(view, dict) and view.get("type") == HOME_VIEW_TYPE
                for view in config.get("views", [])
            ):
                failures.append(f"{url_path or 'unnamed'}: {error}")
            continue
        if planned is None:
            continue
        state, after, view_index = planned
        matches.append(DashboardPlan(url_path, deepcopy(registry), deepcopy(config), after, state, view_index))
    if failures:
        raise MigrationError("Unsupported Van Gogh dashboard schema: " + "; ".join(failures))
    if not matches:
        raise MigrationError("No supported Installer-managed Van Gogh dashboard was discovered")
    if len(matches) != 1:
        raise MigrationError("Multiple supported Van Gogh dashboards were discovered; migration is ambiguous")
    return matches[0]
