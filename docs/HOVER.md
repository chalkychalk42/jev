# Cursor observations

Schema 8 appends five paint-only observations for a bounded targeting measurement.
The proposed body/corpse point comes from screen evidence; the addon only reports what
the stock client says is under the pointer. It does not move the pointer, select a unit,
execute an interaction, or inject a target.

| Field | Observation | Absence and unavailable data |
|---|---|---|
| `cursor.has` | `UnitExists('mouseover')` | A returned nil/false/0 means false; an unavailable or failing API means unknown. |
| `cursor.name_id` | Hash of `UnitName('mouseover')` | Unknown without an observed mouseover or readable name. Names are not unique identities. |
| `cursor.dead` | `UnitIsDead('mouseover')` | Unknown without an observed mouseover; nil/false/0 from a present unit means living. |
| `cursor.is_target` | `UnitIsUnit('mouseover', 'target')` | False without an observed mouseover; an unavailable or failing equality API means unknown. Matching names do not establish equality. |
| `cursor.world` | `GetMouseFocus() == WorldFrame` | Another focus frame means false. Missing focus, `WorldFrame`, or API means unknown. |

All five are raw radio values. Schema 6 and 7 captures retain their original checksum
boundaries and decode these appended fields as unknown. The payload grows from 1035 to
1059 bits; the existing 12-column, 9-row grid is unchanged.

## Source evidence

Inspected the installed client's own `C:\Games\WoW243\Data\enUS\patch-enUS.MPQ`
read-only on 22 September 2026, selecting patched files before the base archive:

- `Interface\FrameXML\GameTooltip.xml`, lines 11–14: `OnTooltipSetUnit` uses
  `this:IsUnit("mouseover")` and colours the text with `GameTooltip_UnitColor("mouseover")`.
- `Interface\FrameXML\SecureStateHeader.lua`, lines 933–940: the stock hover helper
  calls `GetMouseFocus()` and follows frame parents.
- `Interface\FrameXML\TargetFrame.lua`, line 257 and other call sites: stock frames
  use `UnitIsUnit` for equality. JevRadio already uses this API for target-of-target.
- `Interface\FrameXML\UnitFrame.lua`, lines 91–99: UI unit frames explicitly call
  `GameTooltip:SetUnit(self.unit)`. A unit tooltip alone does not prove a world-body hover.

The extracted GameTooltip.xml SHA-256 was
`f9ff294c7ea434549e84b541cfda30c68afc59fbf5fc5649882fe49cd4a11b5f`;
SecureStateHeader.lua was
`caacac0d411f24bac74ab70bff3b526cc3844f01d650eb88dbb94d0ca866903c`.
The extracted client files are inspection evidence and are not distributed in this repo.

## Live measurement and boundary

The supervised `captures/targeting/wolf-hover-1` measurement on 22 September 2026
compared a selected wolf's visible plate, body proposals and a UI portrait. Each probe
waited for a new radio paint after pointer arrival. Coordinates below are client-frame
pixels.

| Observed region | Result | What it establishes |
|---|---|---|
| Selected wolf's nameplate at (1250,481) | `cursor.has`, `cursor.is_target` and `cursor.world` all true | Native nameplates satisfy the same ownership checks as a body point. |
| Early proposals visibly over the body at (1250,561) and (1218,545) | The same three fields all true | The client reported selected-unit ownership; no interaction outcome was tested. |
| UI portrait | `cursor.world` false | This stock UI frame is distinguishable from world focus. |
| An older below-plate proposal at (1205,560), clear terrain after the wolf moved | Ground | A previously proposed point can become empty while the pointer travels. |

**Hover is measured selected-unit ownership, not permission to click a body.** In
particular, `GetMouseFocus() == WorldFrame` does not exclude a native nameplate on this
client. The proposed hover consumer is therefore intentionally not enabled in production
Fight, Interact or Loot. Accepting `MATCH` as a body click would retain the original
nameplate error behind a stronger-sounding predicate.

Hover equality also does not prove range, facing, lootability, an opened shop or damage.
Those remain observed action outcomes. Body-versus-plate discrimination, corpse behavior
and another same-name creature still need their own measurements. Fresh radio sequences
prevent using a paint captured during pointer movement; they do not freeze a moving unit.

The second bounded session, `captures/targeting/wolf-hover-2`, confirmed world-without-unit
at the visually empty point (1200,300) and UI at (320,55). Its proposed other-wolf points
were unavailable or missed moving geometry and returned ground. They do not validate the
same-name `OTHER` case; that remains covered offline only.

The final sighting proposal at (1064,592) also returned `MATCH`. It lies on the wolf's
back in `screenshots/000026-20260922T155312.840876Z.png` (sequence 90), but on terrain above
and to its right after movement in `screenshots/000028-20260922T155313.545505Z.png`.
That after-image and the radio result both carry sequence 97. This is not a visually
confirmed body point: matching paint sequence does not resolve whether the client's pick
region extends beyond the visible model or mouseover data lagged behind movement. The
cause remains unknown, and this sample cannot justify body-click permission.

Saved wolf screenshots 53→54 from `20260922T131407-487841` show one promising additional
visual signal: subtracting the pre-selection colour mask from the post-selection mask
leaves the real ring at (792,399) while suppressing static grass. The same operation on
moving views produces false terrain components. Selection differencing therefore needs
event captures and measured view stability before it can supply target ownership.

## Stock blocking dialogs

The visible load-error dialog in `captures/targeting/ready-schema8/final.png` exposed a
separate observation error: `ui.modal` was false. The installed stock
`Interface\\FrameXML\\UIParent.lua:926` reports addon load failures through `message()`;
`BasicControls.xml:38` implements that function by showing the frame `ScriptErrors`,
declared at line 105. The addon had checked the nonexistent name `ScriptErrorsFrame`.

`MODAL_UP` now observes `ScriptErrors`, covering every message and script error displayed
through this stock dialog. Real Lua wire tests cover visible/hidden states and an error
dialog over a ready merchant. This correction adds no dismissal behavior or schema field.
Live verification in `captures/targeting/modal-wire-verification` observed `ui.modal`
change from false before reloading to true with the same stock error dialog visible.
After a reviewed click closed it, `captures/targeting/modal-cleared-verified` recorded
`ui.modal=false`. The shared modal observation is therefore verified in this live case;
the targeting limitations above remain separate.
