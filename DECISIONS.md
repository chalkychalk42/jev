# Decision log

Every architectural decision, with what triggered it and when to revisit. **Superseded
rows are kept deliberately so we do not re-litigate them.** Read this before proposing an
alternative — it may already have been rejected, with a reason.

---

| # | Date | Decision | Why | Status / revisit when |
|---|---|---|---|---|
| **V1** | 20 Sep 2026 | **New codebase.** No code is carried in from anywhere | The factory architecture — GuideGraph, arm-don't-press, distillation — wants its own seams. Grafting it onto an existing shape costs more than laying them fresh | **Firm** |
| **V2** | 20 Sep 2026 | **Data and knowledge packs may be imported; nothing else may.** `data/knowledge`, `data/sources`, `data/guides`, the zone and name tables | Facts about TBC are expensive to assemble and cheap to verify. Code is the opposite | **Firm** — provenance in `data/README.md` |
| **V3** | 20 Sep 2026 | **JevRadio is pixel-encoded** — RGB grid, 4 bits/channel, calibration row, checksum plus sequence counter | Density and decode speed, at a robustness level that survives gamma and capture transforms | **Current** — revisit if organisers object to opaque addons; the fallback is legible digits read by a glyph atlas, which costs cells not correctness |
| **V4** | 20 Sep 2026 | **Claude is everything above System 1, over the subscription** (`claude -p`), **not the Anthropic API.** GLM is the cheap bulk rung | Owner has a subscription and no Anthropic key, and wants the ML takeover to be real rather than aspirational. Forces the wean onto the critical path, which is the correct place for it | **Current** — revisit if a hosted key appears, or if subscription limits block a gate |
| **V5** | 20 Sep 2026 | **The GuideGraph is generated from this server's world DB, then verified by walking** | 6,599 quests and 109,358 spawns with exact coordinates are already on disk. Generated *from* this server beats recorded *on* it. The recorder pass shrinks to routes, hazards and grind boxes — what only walking can answer | **Current** — `ARCHITECTURE.md` §8. Supersedes the hand-authoring mechanism in PLAN §7.6, not its intent |
| **V6** | 20 Sep 2026 | **Retail guide packs are a cross-check, never a source** | Custom servers delete, rename and relevel quests. Diffing pfQuest/RestedXP against the world DB is how those changes announce themselves | **Firm** |
| **V7** | 20 Sep 2026 | **Multi-client input path is deferred**; the architecture keeps it a deployment question | One cursor per Windows session is an OS constraint. Separate sessions, VMs, background input and dongles each cost differently, and one clean client beats ten bad ones for the learning goal | **Deferred** — decide before Gate B. Until then: no shared client state, `hid` backends swappable |
| **V8** | 20 Sep 2026 | **Rungs are named for their job, never their supplier** — `coach/`, `teacher/`, not `jev/jev/` or `claude/` | A vendor-named module forces a rename the moment the vendor moves, and that rename touches contracts | **Firm** |
| **V9** | 20 Sep 2026 | **Label by outcome, not by authorship.** Train only on choices graded good at +60 s | Cloning a teacher caps the student at the teacher's error rate, and we have ground truth. Promotes PLAN §12.1's optional bandit to load-bearing | **Firm** — `ARCHITECTURE.md` §2 |
| **V10** | 20 Sep 2026 | **`situation_key` in the schema from commit one** | It is simultaneously the teacher-dedup key, the counterfactual join, the agreement bucket and the answer cache. Unrecoverable if added late | **Firm** — bin widths versioned separately |
| **V11** | 20 Sep 2026 | **The teacher's preferred output is a durable artifact** — skill draft, graph patch, `on_fail` edge — not an immediate action | A decision helps one client once; a skill helps forever. On a rate-limited teacher that compounds. Inverts PLAN §9.2 | **Firm** |
| **V12** | 20 Sep 2026 | **The radio labels the vision heads.** Every `addon_ok` tick logs `(vision_estimate, radio_truth)` for every shared field | Makes fusion confidence measured rather than guessed, and makes addon-off a continuously validated mode rather than a hoped-for one | **Firm** — `ARCHITECTURE.md` §5 |
| **V13** | 20 Sep 2026 | **One source of truth for anything two components must agree on.** Python defines, Lua is generated | Agreement by convention decays silently; agreement by construction cannot | **Firm** — hand-editing `Fields.lua` is a build error |

| **V14** | 20 Sep 2026 | **The teacher's model is pinned, and pinned down: Sonnet, never the CLI default** | An unpinned `claude -p` inherits the user's default, which here is Opus — the scarcest model on the plan and the one the human is using to build with. A farm of ten clients would quietly consume the developer's own capacity. Measured: unpinned calls returned 429 "out of usage credits" while Sonnet and Haiku answered immediately; the *plan* was untouched, only the Opus allocation was spent, and the error message said neither | **Firm** — an explicit model still wins, for escalating a hard postmortem |
| **V15** | 20 Sep 2026 | **Wait for the artifact; discard the stale action** | Measured round trip on a realistic prompt is **52 s** against a 60 s situation bin, so a late answer is the common case. Artifacts describe the *step* and do not go stale; only the fallback action does, and by the time it lands the scripted coach has acted anyway. Discarding whole replies to avoid stale actions would throw away the durable half — which V11 says is the valuable half | **Firm** — `stale_after_s` on the runtime |
| **V16** | 20 Sep 2026 | **Outstanding questions are tracked by the bucket they were asked under** | `situation_key` bins step age, so a step crosses from "fresh" to "slow" at 60 s and its key changes mid-flight. Looking up only the *current* key misses every slow answer, re-asks, and never hits the cache the key exists to enable | **Firm** |

| **V17** | 20 Sep 2026 | **There is no facing API in 2.4.3. Heading is measured, not read** | `GetPlayerFacing` arrived in 3.0; on this client it does not exist, so the field decodes as unknown — confirmed live, and only our own addons reference it while the stock UI does not. Navigation therefore uses **closed-loop turning**: take a heading from a position delta while moving, compute the angle to the target, turn for `angle / turn_rate`, re-measure. Absolute facing is never needed, and a noisy reading costs another iteration rather than a walk into a lake | **Current** — revisit only if a vision head can read minimap rotation, which needs rotate-minimap enabled |
| **V18** | 20 Sep 2026 | **The bot never fights the operator for the desktop** | `SetForegroundWindow` fails from a process that does not own the foreground — measured. `AttachThreadInput` is used so a supervisor can restore a window after a loading screen or a stray alt-tab, but `Hid` refuses to press anything whenever the window is not focused, and that guard is not conditional on it | **Firm** — the guard held on its first live test, refusing rather than typing into the wrong window |

### Standing constraint
**Windows has one system cursor per session.** No input method — `SendInput`, a dongle,
anything — lets the bot play while you use the same desktop. See V7.

### Carried over from the plan, unchanged
No DLL injection, no memory read/write, no packet forge, no speed or teleport. Control is
virtual HID; sense is screen capture plus an addon that paints and never actuates. Offline
server only. (PLAN §2.)
