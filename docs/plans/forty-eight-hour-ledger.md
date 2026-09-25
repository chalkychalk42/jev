Forty-eight-hour ledger
======================

The running state of `docs/plans/forty-eight-hour-session.md`, newest last in each section.
Read "Now" after every compaction. Times are BST (the Windows clock runs about 25 s ahead).

Now
---

- **25 Sep 14:10.** Pre-flight A is under way, with the loop stopped and the client closed.
  - The operator logged Testvvi out at 09:13, in Goldshire, at level 13.
  - T-0 is Friday 19:30.
  - The campaign is `var/campaign.json`: Testvvi until level 20 or Saturday 10:30, then the
    human mage Itheamar (row 3, not made yet).
- **Pre-flight A done:**
  - A1 is the repo loop, the keeper and the status screen (58fa756).
  - A2 (a) is the band rule, V162 (bde61ca).
  - A3 put quests 16, 21, 40 and 60 into Testvvi's playhead (not in git; `var/`).
  - A4 is the campaign tool and the create screen, unmeasured (486f754).
  - The danger band from L-1 to L+2 (b02409a).
- **Pre-flight A open:**
  - A8, the caster profile: its design is being written.
  - A5, the scoreboard.
  - A6, the flaky test.
  - A2 (b) and (c) are deferred: neither character reaches the end of the 12-20 guide inside
    the window.
- **At T-0 (§6):**
  - B1 is the state checks.
  - B2 is the V158-V162 validation: two sessions on Testvvi.
  - B3 measures the glue screens (`tools/character.py measure --focus`), sets the constants in
    `jev/clients/session.py`, then `tools/character.py enter` makes Itheamar. One session
    follows, then `tools/keep.sh client-restart` and `tools/character.py enter --name Testvvi`.
  - B4 starts the loop, installs the keeper timer (`tools/keep.sh install-timer`) and starts
    the heartbeat.

Blocks
------

| Block | Hours | Character | Level from-to | XP/h | Deaths/h | Stuck/h | Quests/h | Tutor calls/h | Change under trial | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|

Trials
------

| Change | Deployed | Predicted effect (metric) | Proof it fired (log line) | Judged | Kept/reverted |
|---|---|---|---|---|---|

Issues
------

| Seen | Issue | Evidence | State |
|---|---|---|---|
