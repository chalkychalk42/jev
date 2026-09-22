# Inventory and merchant service

`jev.clients.vendor.Vendor` uses the existing physical Interact composition to reach a
merchant, then right-clicks observed stock inventory and merchant buttons. The addon
only reads and paints. No item destruction, macros, chat commands, or addon actuation
are part of this service.

The stock UI behavior was checked against the preserved Blizzard 2.4.3 sources:

- [ContainerFrame.lua](https://github.com/MOUZU/Blizzard-WoW-Interface/blob/master/2.4.3/FrameXML/ContainerFrame.lua):
  container and button IDs identify the actual bag/slot; right-click uses that slot.
  Button display order differs from slot order.
- [MerchantFrame.lua](https://github.com/MOUZU/Blizzard-WoW-Interface/blob/master/2.4.3/FrameXML/MerchantFrame.lua):
  showing a merchant opens the backpack, offers are ten per page, stock item button
  IDs identify offers, and right-click buys the normal offer. Extended-cost offers
  take a separate confirmation path and are refused here.
- [GossipFrame.lua](https://github.com/MOUZU/Blizzard-WoW-Interface/blob/master/2.4.3/FrameXML/GossipFrame.lua):
  gossip options arrive as localized text/type pairs. A unique `vendor` option is
  bound to its visible stock button, then selected with the existing list composition.

Schema 7 appends precise copper, one cycling inventory slot, one cycling visible
merchant offer, and exact food/drink identities and bag totals. It retains explicit
schema-6 decoding for historical recordings and older installed addons; their new
fields remain unknown. A schema-6 addon cannot authorize the new vendor transactions.
The grid is 12 columns by 9 rows. `Supplies.lua` must be installed together with the
updated TOC, Helpers, Fields and painter when the addon is next deployed.

Every paint scans carried bags 0–4 and derives food/drink totals from the exact
starting-profile item IDs. A texture whose item link is not readable invalidates
these totals. Zero therefore means the known item was observed absent, not that an
inventory scan failed. The cycling slot is recorded as one `Bags.slot`; it is never
claimed to be a complete inventory.

Selling requires all of: a generated allowlisted item ID, observed poor quality,
known positive stack count, unlocked slot, exact merchant identity, normal buy tab,
empty cursor, no held modifier keys, and inactive single-item repair mode. The body
re-reads the slot before clicking. It opens a closed bag only through that bag's
observed stock toolbar button. A sale is confirmed only when the exact generated
stack sale value appears in copper, a slot becomes free, and the bag revision changes.
Unchanged or conflicting evidence ends the service; it does not retry the sale blindly.

Purchases use the offer's current copper price and package quantity, so observed
discounts are respected. The body buys only requested exact item identities, preserves
the configured cash reserve, and verifies both the money debit and increased item
total after each click. It visits observed pages and rewinds before a new supply search.
Blind reads, missing stock, unaffordable offers, changed identities, refused input and
non-changing transactions have named bounded exits. External cancellation propagates
without a cleanup read or Escape; normal cleanup closes only an observed merchant.

Facts are regenerated from this server's database and the generated combat profiles:

```sh
.venv/bin/python tools/gen_combat_profiles.py
.venv/bin/python tools/gen_vendor_catalog.py
.venv/bin/python tools/gen_addon_fields.py
```

`vendor-catalog.json` contains 516 conservative junk identities with unit sale values,
2266 merchant spawns, their unconditional cash inventories, and exact starting-profile
supplies. The Python catalog and Lua supply table are emitted by one generator. Tests
regenerate them byte-for-byte and check all junk against every quest requirement,
source and reward reference in the local DB.

The sell allowlist deliberately excludes all weapons, armor, consumables, containers,
ammunition, quest items and quest references, even if poor quality. There is no automatic
equipment valuation yet. A full inventory containing no eligible junk stops explicitly.
Restocking currently covers starting-profile food/drink only: ammunition, reagents,
higher-level replacements, action-bar changes, conditional/extended-currency stock and
ambiguous or off-wire gossip branches remain unsupported. Supplier paths, stock UI
layout and the complete transaction sequence have device-fake and real-Lua wire tests;
the new merchant composition still needs acceptance on the actual client.
