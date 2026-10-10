# V613: full bags cannot rearm a quest gather indefinitely

Candidate `bet-gather-capacity`, based on deployed control `8d39836`. This is shared
`LiveBody` behavior, inherited by the hive adapter; no new server observation or action is
required. It is a separate trial from the currently playing quest/transport candidate.

## Recorded failure

In `20261010T094926-64493c`, hive-879 (level 17 orc warlock) returned
`GRIND_UNTIL | preempted | bags are full; the object would give nothing` **1,462 times**
while working Minshina's Skull (808). Its bags had zero free slots. A fixture retains the
observed inventory, vitals and held quest from that run. Other sampled sessions repeat the
same failure hundreds of times.

`Gather.pick` correctly refused a pickup with no room, but `LiveBody._result` treated that
as a preemption. The worker's bounded retries count aborted or timed-out attempts, not
preemptions. When the bag service was already blocked or unreachable, the policy returned
to the same gather without consuming an attempt. The creature hunt already handles full
bags with an unavailable service; object gathering lacked the corresponding exit.

## Change

- Check completion and current observations before walking to each object. Full bags
  request no walk or object click; combat, death, a dialog or an unread observation still
  preempt without spending an attempt.
- With an available bag service, yield normally so that service can make room.
- When the existing policy says that service cannot help, return an aborted `bags_full`
  result. The normal retry limit and route fallback now apply. The quest stays held and
  uncompleted; a freed slot allows a later gather to succeed.
- Handle bags filling during the approach in the same way. No global change to the
  `bags_full` result of combat, looting or creature hunting is made.

## Validation and measurement

The focused tests exercise both body and the actual worker/runtime, with fake movement and
input. The recorded inventory plus blocked service now takes the route's existing fallback
after three failed gathers. Three regressions fail against unchanged `8d39836`: no-junk
capacity, unreachable service and bounded worker fallback. Completion and interruptions
retain their distinct meanings. Full-suite reports accompany the queued exact commit.

This proves the control-flow repair, not a measured XP/hour gain. Judge a fresh trial on
speed with the existing death/idle/stuck guards. Count both preempted and aborted gathers
when examining retries: a relabeled failure alone is no improvement. Inspect route
progress, successful collections and retained quests alongside the total leveling rate.
If an earlier trial moves the control, rebuild and test the merged tree before play.

## Context from the pull/sweep review

The corrected pull/sweep trial `149a479` ended on 10 October at 11:13 BST, unmerged. Its
adjusted speed-index difference was +0.1225 (90% interval [+0.0393, +0.1791]), about 16.3%
of the control baseline. The required death reduction was not established: ratio 0.856,
90% [0.723, 1.009]. The stuck-event ratio was 1.292 [0.985, 1.709], an uncertain increase.
Its target is not changed retrospectively.

A cached diagnostic breakdown of that window finds more rest timeouts in the candidate
arm (3.59 versus 2.03 per played hour), rather than more unreachable hunts (1.76 versus
2.12). These pooled counts include all characters and are not baseline-adjusted estimates.
One level 19 hunter's session repeated 48 dry, stalled-mana waits while sick after a healer
resurrection. That recovery issue remains separate. The full-bag gather loop occurs in
both arms and is the narrow shared repair built here; it does not explain the entire
pull/sweep result or establish why the arms differ.
