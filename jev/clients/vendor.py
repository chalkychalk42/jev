"""Sell confirmed junk and buy exact profile supplies through observed stock buttons.

The addon paints bag slots and merchant offers. This body chooses; stock right-clicks
execute. A click earns nothing: the money and inventory changes must both be observed.
No destroy-items path, guessed coordinates, modifier clicks or purchase popups exist.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from jev.perceive.radio_frame import name_id
from jev.run.evidence import event, operation, traced
from jev.world.vendor import Supply, junk_prices

# Grey, white and green: what the price tables may offer for sale. Never blue or better.
SELL_QUALITIES = (0, 1, 2)


class Vended(StrEnum):
    DONE = "done"
    NOT_NEEDED = "not_needed"
    NO_VENDOR = "no_vendor"
    WRONG_VENDOR = "wrong_vendor"
    NO_JUNK = "no_junk"
    UNAVAILABLE = "unavailable"
    TOO_POOR = "too_poor"
    NO_BUTTON = "no_button"
    NO_CHANGE = "no_change"
    BLIND = "blind"
    INTERRUPTED = "interrupted"
    REFUSED = "refused"
    TIMEOUT = "timeout"

    @property
    def ok(self) -> bool:
        return self in (Vended.DONE, Vended.NOT_NEEDED)


class _Stop(Exception):
    def __init__(self, result: Vended, detail: str):
        super().__init__(detail)
        self.result, self.detail = result, detail


@dataclass
class Vendor:
    hid: object
    read: Callable[[], dict | None]
    visit: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    eligible: Mapping[int, int] = field(default_factory=junk_prices)
    # Injectable clocks make refusal and settle deadlines testable without real sleeps.
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    sold_stacks: int = field(default=0, init=False)
    bought_units: int = field(default=0, init=False)
    detail: str = field(default="", init=False)
    _deadline: float = field(default=0, init=False)
    _merchant: int = field(default=0, init=False)

    @traced("vendor")
    def run(self, *, expected_name: str, supplies: tuple[Supply, ...] = (),
            sell: bool = True, min_free: int = 6, reserve_copper: int = 0,
            timeout_s: float = 90.0) -> Vended:
        """Reach `min_free`, then top up requested exact items in one merchant visit.

        `expected_name` comes from a generated merchant spawn. Empty/unconfirmed names
        are refused. Supply identities come from `supplies_for` (or an explicit caller
        configuration); the body never upgrades to a different item that the bar cannot use.
        """
        self.sold_stacks = self.bought_units = 0
        self.detail = ""
        with operation("vendor.request") as span:
            if span.enabled:
                span.finish(code="requested", data={"name": expected_name, "sell": sell,
                    "min_free": min_free, "reserve_copper": reserve_copper,
                    "supplies": [{"item_id": s.item_id, "desired": s.desired} for s in supplies]})
        if not expected_name or min_free < 0 or reserve_copper < 0 or timeout_s <= 0:
            self.detail = "invalid or missing vendor contract"
            return Vended.REFUSED
        if any(s.item_id <= 0 or s.desired <= 0 for s in supplies):
            self.detail = "invalid supply identity or quantity"
            return Vended.REFUSED
        self._merchant = name_id(expected_name)
        self._deadline = self.clock() + timeout_s
        interrupted = False
        try:
            self._safe(self.read())
            if not self.visit():
                raise _Stop(Vended.NO_VENDOR, "could not open the selected merchant")
            self._await(lambda r: r.get("ui.vendor") is True, 4.0, merchant=False)
            if sell:
                self._sell(min_free)
            for supply in supplies:
                self._buy(supply, reserve_copper)
            return Vended.DONE if self.sold_stacks or self.bought_units else Vended.NOT_NEEDED
        except _Stop as stop:
            self.detail = stop.detail
            return stop.result
        except BaseException:
            # Cooperative cancellation/focus refusal must propagate unchanged. Reading
            # again can raise a different exception; Escape must not be sent afterward.
            interrupted = True
            raise
        finally:
            # Escape is a toggle: only close a currently observed matching merchant,
            # never press it blindly on a disappeared frame or over a popup.
            latest = None if interrupted else self.read()
            if (latest and latest.get("ui.vendor") is True and latest.get("ui.modal") is False
                    and latest.get("merchant.name_id") == self._merchant):
                self.hid.tap("esc")

    def _safe(self, values: dict | None) -> dict:
        if values is None:
            raise _Stop(Vended.BLIND, "inventory or merchant radio unreadable")
        if values.get("ui.modal") is True or values.get("vitals.combat") is True:
            raise _Stop(Vended.INTERRUPTED, "combat or a blocking dialog interrupted the merchant")
        if values.get("vitals.dead") is True or values.get("vitals.ghost") is True:
            raise _Stop(Vended.INTERRUPTED, "merchant service interrupted by death")
        required = ("ui.modal", "vitals.combat", "vitals.dead", "vitals.ghost")
        if any(values.get(k) is not False for k in required):
            raise _Stop(Vended.BLIND, "merchant service requires observed safe life/UI state")
        if self.clock() >= self._deadline:
            raise _Stop(Vended.TIMEOUT, "merchant service deadline exhausted")
        return values

    def _read(self, *, merchant: bool = True) -> dict:
        values = self._safe(self.read())
        if merchant:
            if values.get("ui.vendor") is not True:
                raise _Stop(Vended.NO_VENDOR, "merchant frame closed during service")
            observed = values.get("merchant.name_id")
            if observed is None:
                raise _Stop(Vended.BLIND, "merchant identity/tab is unreadable")
            if observed != self._merchant:
                raise _Stop(Vended.WRONG_VENDOR, "observed merchant differs from selected spawn")
            if values.get("merchant.ready") is not True:
                raise _Stop(Vended.REFUSED, "merchant cursor/modifiers/repair mode is not confirmed ready")
        return values

    def _await(self, predicate, seconds: float, *, merchant: bool = True) -> dict:
        until = min(self._deadline, self.clock() + seconds)
        while self.clock() < until:
            values = self._read(merchant=merchant)
            if predicate(values):
                return values
            self.sleep(0.05)
        raise _Stop(Vended.NO_CHANGE, "no confirmed transaction or UI change before deadline")

    def _click(self, values: dict, prefix: str, *, right: bool = False) -> None:
        x, y = values.get(prefix + "x"), values.get(prefix + "y")
        if x is None or y is None or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise _Stop(Vended.NO_BUTTON, f"no observed stock button: {prefix}")
        ox, oy = self.window_origin
        w, h = self.window_size
        event("vendor.click", data={"prefix": prefix, "right": right,
                                    "point": [ox + round(x * w), oy + round(y * h)],
                                    "merchant_name_id": values.get("merchant.name_id")})
        if self.hid.click(ox + round(x * w), oy + round(y * h), right=right) is False:
            raise _Stop(Vended.REFUSED, "input device refused merchant click")

    @traced("vendor.equip_bags")
    def equip_bags(self, bags: Mapping[int, int], *, timeout_s: float = 20.0) -> int:
        """Put any general bag lying in the bags into a free bag slot; how many went on.

        A right-click on a bag in the inventory equips it - but only with no shop open,
        where the same click sells it. A new character's backpack has sixteen slots and a
        loot table's Small Green Pouch rode in it all night, six slots unused, while full
        bags sent the character to a merchant again and again.
        """
        self.detail = ""
        self._deadline = self.clock() + timeout_s
        equipped = 0
        seen: set[int] = set()
        try:
            while True:
                values = self._read(merchant=False)
                if values.get("ui.vendor") is not False:
                    return equipped                      # a shop open: a click would sell
                total, ordinal = values.get("inventory.total"), values.get("inventory.ordinal")
                if total is None or ordinal is None:
                    raise _Stop(Vended.BLIND, "bag slot census unreadable")
                seen.add(ordinal)
                if (values.get("inventory.item_id") in bags
                        and values.get("inventory.locked") is False):
                    if values.get("inventory.x") is None:
                        self._click(values, "inventory.open_")
                        bag = values.get("inventory.bag")
                        self._await(lambda r, bag=bag: r.get("inventory.bag") == bag
                                    and r.get("inventory.x") is not None, 4.0, merchant=False)
                        continue
                    self._click(values, "inventory.", right=True)
                    self._await(lambda r, before=total: (r.get("inventory.total") or 0) > before,
                                4.0, merchant=False)
                    equipped += 1
                    seen.clear()
                    continue
                if len(seen) >= total:
                    return equipped
                self.sleep(0.05)
        except _Stop as stop:
            self.detail = stop.detail
            return equipped

    @traced("vendor.bag_items")
    def bag_items(self, *, timeout_s: float = 10.0) -> set[int] | None:
        """Every item id in the bags, from one full census; `None` if it did not complete."""
        self._deadline = self.clock() + timeout_s
        seen: dict[int, int] = {}
        try:
            while self.clock() < self._deadline:
                values = self._read(merchant=False)
                total, ordinal = values.get("inventory.total"), values.get("inventory.ordinal")
                if total is None or ordinal is None:
                    return None
                seen[ordinal] = values.get("inventory.item_id") or 0
                if len(seen) >= total:
                    return {item for item in seen.values() if item}
                self.sleep(0.05)
        except _Stop:
            return None
        return None

    @traced("vendor.equip_items")
    def equip_items(self, items: set[int], *, timeout_s: float = 30.0) -> list[int]:
        """Put on each of these items found in the bags; the ones that went on.

        The same right-click that equips a bag: with no shop open, where it would sell. An
        item that binds when worn asks first, and the popup's first button accepts. Put on
        is the bag slot's contents changing (the worn item it replaced lands there).
        """
        self.detail = ""
        self._deadline = self.clock() + timeout_s
        wanted, done = set(items), []
        seen: set[int] = set()
        try:
            while wanted:
                values = self._read(merchant=False)
                if values.get("ui.vendor") is not False:
                    return done                          # a shop open: a click would sell
                total, ordinal = values.get("inventory.total"), values.get("inventory.ordinal")
                if total is None or ordinal is None:
                    raise _Stop(Vended.BLIND, "bag slot census unreadable")
                seen.add(ordinal)
                item = values.get("inventory.item_id")
                if item in wanted and values.get("inventory.locked") is False:
                    if values.get("inventory.x") is None:
                        self._click(values, "inventory.open_")
                        bag = values.get("inventory.bag")
                        self._await(lambda r, bag=bag: r.get("inventory.bag") == bag
                                    and r.get("inventory.x") is not None, 4.0, merchant=False)
                        continue
                    revision = values.get("inventory.revision")
                    self._click(values, "inventory.", right=True)
                    self._await_worn(revision)
                    wanted.discard(item)
                    done.append(item)
                    seen.clear()
                    continue
                if len(seen) >= total:
                    return done
                self.sleep(0.05)
        except _Stop as stop:
            self.detail = stop.detail
        return done

    def _await_worn(self, revision, seconds: float = 4.0) -> None:
        """The bags changing after an equip click, answering "will bind it to you" on the
        way. Read raw: every other read here refuses a dialog, and this one is expected."""
        until = min(self._deadline, self.clock() + seconds)
        accepted = False
        while self.clock() < until:
            values = self.read()
            if values is None:
                raise _Stop(Vended.BLIND, "inventory radio unreadable after the equip click")
            if (values.get("vitals.combat") is True or values.get("vitals.dead") is True
                    or values.get("vitals.ghost") is True):
                raise _Stop(Vended.INTERRUPTED, "combat or death while putting an item on")
            if values.get("ui.modal") is True and not accepted:
                self._click(values, "ui.advance_")        # the popup's first button: Okay
                accepted = True
            elif values.get("ui.modal") is not True and values.get("inventory.revision") != revision:
                return
            self.sleep(0.05)
        raise _Stop(Vended.NO_CHANGE, "the bags did not change after the equip click")

    @staticmethod
    def _slot(values: dict) -> tuple:
        return tuple(values.get("inventory." + k)
                     for k in ("bag", "slot", "item_id", "count", "revision"))

    @traced("vendor.sell")
    def _sell(self, min_free: int) -> None:
        seen: set[int] = set()
        revision = None
        while True:
            values = self._read()
            free, total = values.get("bags.free"), values.get("inventory.total")
            ordinal = values.get("inventory.ordinal")
            if free is None or total is None or ordinal is None:
                raise _Stop(Vended.BLIND, "bag capacity or slot census unreadable")
            if free >= min_free:
                return
            if values.get("inventory.revision") != revision:
                revision, seen = values.get("inventory.revision"), set()
            seen.add(ordinal)
            item_id, count = values.get("inventory.item_id"), values.get("inventory.count")
            price = self.eligible.get(item_id, 0)
            eligible = price > 0 and values.get("inventory.quality") in SELL_QUALITIES
            eligible = eligible and values.get("inventory.locked") is False and count is not None and count > 0
            if eligible:
                # Re-read immediately before input. An asynchronously changed slot or
                # bag layout invalidates the old click and returns us to observation.
                fresh = self._read()
                if self._slot(fresh) != self._slot(values):
                    continue
                if (fresh.get("inventory.quality") not in SELL_QUALITIES
                        or fresh.get("inventory.locked") is not False):
                    continue
                if fresh.get("inventory.x") is None:
                    self._click(fresh, "inventory.open_")
                    bag_id = fresh.get("inventory.bag")
                    self._await(lambda r, bag=bag_id: r.get("inventory.bag") == bag
                                and r.get("inventory.x") is not None,
                                max(4.0, total * 0.2 + 2.0))
                    continue
                before = fresh.get("bags.money_copper")
                if before is None or revision is None:
                    raise _Stop(Vended.BLIND, "exact copper/revision required before selling")
                event("sale.request", data={"item_id": item_id, "count": count,
                      "unit_price": price, "money_copper": before, "free": free,
                      "revision": revision})
                self._click(fresh, "inventory.", right=True)
                # Stock right-click sells the whole stack. Require its exact generated
                # sale value, one newly free slot, and a real bag-update event.
                observed = self._await(lambda r, cash=before + price * count, slots=free, rev=revision:
                            r.get("bags.money_copper") == cash
                            and r.get("bags.free") is not None and r["bags.free"] > slots
                            and r.get("inventory.revision") is not None
                            and r["inventory.revision"] != rev, 4.0)
                event("sale.observed", data={key: observed.get(key) for key in (
                    "bags.money_copper", "bags.free", "inventory.revision")})
                self.sold_stacks += 1
                seen.clear()
                continue
            if len(seen) >= total:
                if self.sold_stacks:
                    # Short of the target, but every sale-eligible stack is gone: the bags
                    # have room again, and the policy asks for more when they are full.
                    return
                raise _Stop(Vended.NO_JUNK, "no confirmed sale-eligible junk can make enough bag space")
            self.sleep(0.05)

    @traced("vendor.buy")
    def _buy(self, supply: Supply, reserve: int) -> None:
        # Each item search begins at the first page: a previous item's successful
        # purchase may have left us on a later page that does not sell this item.
        while True:
            start = self._read()
            start_page = start.get("merchant.page")
            if start_page is None:
                raise _Stop(Vended.BLIND, "merchant page unreadable")
            if start_page <= 1:
                break
            self._click(start, "merchant.prev_")
            self._await(lambda r, page=start_page - 1: r.get("merchant.page") == page, 4.0)
        pages: set[int] = set()
        offers: set[int] = set()
        page = None
        while True:
            values = self._read()
            current_page, total = values.get("merchant.page"), values.get("merchant.total")
            if current_page is None or total is None:
                raise _Stop(Vended.BLIND, "merchant page or offer count unreadable")
            if current_page != page:
                page, offers = current_page, set()
            # Generated profile totals are available on every row. When buying a
            # configured non-profile item, the matching offer supplies its owned total.
            if values.get(f"bags.{supply.role}_id") == supply.item_id:
                owned = values.get(f"bags.{supply.role}_count")
                if owned is not None and owned >= supply.desired:
                    return
            index = values.get("merchant.index")
            if index is not None:
                offers.add(index)
            if values.get("merchant.item_id") == supply.item_id:
                owned = values.get("merchant.owned")
                price, quantity = values.get("merchant.price"), values.get("merchant.quantity")
                cash = values.get("bags.money_copper")
                if owned is None or price is None or quantity is None or cash is None:
                    raise _Stop(Vended.BLIND, "offer identity/quantity/price/owned total unreadable")
                if owned >= supply.desired:
                    return
                if values.get("merchant.extended") is not False:
                    raise _Stop(Vended.UNAVAILABLE, "extended-currency offers require a separate executor")
                if price <= 0 or quantity <= 0:
                    raise _Stop(Vended.UNAVAILABLE, "invalid or unpriced supply offer")
                if cash - price < reserve:
                    raise _Stop(Vended.TOO_POOR, "supply price would exceed the available purse/reserve")
                if values.get("merchant.unlimited") is not True:
                    stock = values.get("merchant.stock")
                    if stock is None or stock < quantity:
                        raise _Stop(Vended.UNAVAILABLE, "insufficient observed stock")
                if values.get("bags.free") is None or values["bags.free"] < 1:
                    raise _Stop(Vended.NO_JUNK, "a free bag slot is required before a purchase")
                fresh = self._read()
                keys = ("index", "item_id", "owned", "price", "quantity", "page", "extended",
                        "stock", "unlimited")
                if any(fresh.get("merchant." + k) != values.get("merchant." + k) for k in keys):
                    continue
                if fresh.get("bags.money_copper") != cash:
                    continue
                if fresh.get("bags.free") is None or fresh["bags.free"] < 1:
                    continue
                event("purchase.request", data={"item_id": supply.item_id,
                      "owned": owned, "quantity": quantity, "price": price,
                      "money_copper": cash, "desired": supply.desired})
                self._click(fresh, "merchant.", right=True)
                observed = self._await(lambda r, money=cash - price, count=owned + quantity:
                    r.get("bags.money_copper") == money and (
                    (r.get("merchant.item_id") == supply.item_id
                     and r.get("merchant.owned") == count)
                    or (r.get(f"bags.{supply.role}_id") == supply.item_id
                        and r.get(f"bags.{supply.role}_count") == count)),
                    4.0)
                event("purchase.observed", data={key: observed.get(key) for key in (
                    "bags.money_copper", "merchant.item_id", "merchant.owned",
                    f"bags.{supply.role}_id", f"bags.{supply.role}_count")})
                self.bought_units += quantity
                continue
            expected = max(0, min(10, total - (current_page - 1) * 10))
            if len(offers) >= expected:
                pages.add(current_page)
                prefix = "merchant.next_"
                next_page = current_page + 1
                if next_page in pages or next_page < 1 or values.get(prefix + "x") is None:
                    raise _Stop(Vended.UNAVAILABLE, f"merchant does not offer required {supply.name}")
                self._click(values, prefix)
                self._await(lambda r, page=next_page: r.get("merchant.page") == page, 4.0)
            self.sleep(0.05)
