"""Causal child evidence, isolated from strategic credit and safe through shutdown."""

import threading
from enum import StrEnum

import pytest
from test_runtime_records import runtime, seen

from jev.coach.schema import Decision, Intent
from jev.learn.episode import Recorder, SkillOutcome, read
from jev.orch.runtime import Armed
from jev.run.evidence import bind, event, operation, traced
from jev.run.supervisor import Result, Worker
from jev.world.state_v1 import ArmedBy


def arm(client="c"):
    return Armed(Decision(goal="objective", intent=Intent.ADVANCE, skill="GRIND_UNTIL",
                          abort_if=["dead"], confidence=1, why="scripted"),
                 ArmedBy.POLICY, 0, "guide", f"{client}:decision", "before", "bucket",
                 f"{client}:arm")


def rows(recorder):
    return read(recorder.dir / "executions.jsonl")


def test_child_evidence_preserves_binding_and_does_not_create_decisions_or_ticks(tmp_path):
    rec = Recorder(tmp_path)
    original = arm()
    binding = bind(rec, original, client_id="c")
    original.step_id, original.decision_id, original.arm_id = "after", "changed", "changed"
    with binding, operation("hunt") as parent:
        with operation("fight", data={"wanted": 42}) as child:
            event("click", code="accepted", data={"point": [10, 20]})
            child.finish(code="unreachable", data={"closed": 8})
        parent.finish(code="done")
    captured = rows(rec)
    assert len(captured) == 5
    assert {r["arm_id"] for r in captured} == {"c:arm"}
    assert {r["decision_id"] for r in captured} == {"c:decision"}
    assert {r["step_id"] for r in captured} == {"before"}
    assert {r["armed_by"] for r in captured} == {"policy"}
    assert captured[1]["parent_operation_id"] == captured[0]["operation_id"]
    assert captured[2]["parent_operation_id"] == captured[1]["operation_id"]
    assert captured[3]["operation_id"] == captured[1]["operation_id"]
    assert captured[3]["data"] == {"wanted": 42, "closed": 8}
    assert captured[4]["operation_id"] == captured[0]["operation_id"]
    assert all(r["duration_s"] >= 0 for r in captured if r["phase"] == "end")
    assert len({r["event_id"] for r in captured}) == len(captured)
    assert rec.tick_id == 0
    assert not any((rec.dir / f"{s}.jsonl").exists() for s in ("ticks", "decisions", "skills"))
    event("outside")
    assert rows(rec) == captured
    rec.close()


class Native(StrEnum):
    NOTHING = "nothing"


class Primitive:
    detail = "no observed change"

    @traced("primitive")
    def run(self, value):
        if isinstance(value, BaseException):
            raise value
        return value


def test_tracing_reports_native_values_and_exceptions_without_success_labels(tmp_path):
    rec = Recorder(tmp_path)
    primitive = Primitive()
    with bind(rec, arm(), client_id="c"):
        for value in (Native.NOTHING, True, False, None):
            assert primitive.run(value) is value
        with pytest.raises(ValueError, match="missing"):
            primitive.run(ValueError("missing"))
        with pytest.raises(RuntimeError, match="later failure"), operation("unfinished") as span:
            span.finish(code="done")
            raise RuntimeError("later failure")
    ends = [r for r in rows(rec) if r["phase"] == "end"]
    assert [r["code"] for r in ends] == ["nothing", "true", "false", "none", "exception", "exception"]
    assert ends[0]["detail"] == "no observed change"
    assert ends[-1]["data"]["exception_type"] == "RuntimeError"
    assert not any("outcome" in r["data"] for r in ends)
    assert primitive.run(True) is True
    assert len(rows(rec)) == 12
    rec.close()


class Body:
    def __init__(self, *, wait=False, cleanup_error=False):
        self.started = threading.Event()
        self.wait = wait
        self.cleanup_error = cleanup_error
        self.releases = 0

    def execute(self, arm, state, checkpoint):
        with operation("fight") as span:
            self.started.set()
            while self.wait:
                checkpoint()
                threading.Event().wait(0.001)
            span.finish(code="unreachable")
        return Result(SkillOutcome.SUCCEEDED, "objective observed", "done")

    def release(self):
        self.releases += 1
        event("release", data={"held": []})
        if self.cleanup_error:
            raise RuntimeError("release refused")


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_worker_closes_after_release_and_reports_final_cleanup_result(tmp_path, cleanup_error):
    rec = Recorder(tmp_path)
    body = Body(cleanup_error=cleanup_error)
    worker = Worker(body, arm(), seen(), recorder=rec)
    worker.thread.start()
    assert worker.done.wait(2)
    worker.thread.join()
    captured = rows(rec)
    assert body.releases == 1
    assert captured[-2]["operation"] == "release"
    assert captured[-1]["operation"] == "worker" and captured[-1]["phase"] == "end"
    expected = "aborted" if cleanup_error else "succeeded"
    assert captured[-1]["data"]["outcome"] == worker.result.outcome.value == expected
    assert captured[-1]["code"] == ("error" if cleanup_error else "done")
    rec.close()


def test_cancelled_child_keeps_original_context_and_worker_releases_before_done(tmp_path):
    rec = Recorder(tmp_path)
    body = Body(wait=True)
    original = arm()
    worker = Worker(body, original, seen(), recorder=rec)
    original.step_id = "after"
    worker.thread.start()
    assert body.started.wait(2)
    worker.cancel("operator stop")
    assert worker.done.wait(2)
    worker.thread.join()
    captured = rows(rec)
    assert {r["step_id"] for r in captured} == {"before"}
    child = next(r for r in captured if r["operation"] == "fight" and r["phase"] == "end")
    assert child["code"] == "exception" and child["data"]["exception_type"] == "Cancelled"
    assert captured[-1]["data"]["outcome"] == "preempted"
    assert body.releases == 1
    rec.close()


@pytest.mark.parametrize("fail_phase", ["begin", "event", "end"])
def test_logging_errors_cannot_strand_worker_or_skip_release(tmp_path, fail_phase):
    rec = Recorder(tmp_path)
    write = rec.execution
    def broken(row):
        if row.phase == fail_phase:
            raise OSError("disk unavailable")
        write(row)
    rec.execution = broken
    body = Body()
    worker = Worker(body, arm(), seen(), recorder=rec)
    worker.thread.start()
    assert worker.done.wait(2)
    worker.thread.join()
    assert body.releases == 1
    assert worker.result.outcome is SkillOutcome.ABORTED
    assert worker.result.code == "error"
    assert "disk unavailable" in worker.result.detail
    rec.close()


def test_context_and_recorder_are_safe_across_independent_producer_threads(tmp_path):
    rec = Recorder(tmp_path)
    barrier = threading.Barrier(3)
    failures = []
    def produce(client):
        try:
            with bind(rec, arm(client), client_id=client), operation("body"):
                barrier.wait(timeout=2)
                for i in range(100):
                    event("input", data={"client": client, "i": i})
                    rec.next_tick_id()
        except BaseException as exc:
            failures.append(exc)
    threads = [threading.Thread(target=produce, args=(client,)) for client in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert not failures
    captured = rows(rec)
    assert len(captured) == 204 and rec.tick_id == 200
    for row in captured:
        assert row["arm_id"] == f"{row['client_id']}:arm"
        assert row["decision_id"] == f"{row['client_id']}:decision"
        if row["phase"] == "event":
            assert row["data"]["client"] == row["client_id"]
    rec.close()
    with pytest.raises(RuntimeError, match="closed"), bind(rec, arm(), client_id="c"):
        event("late")


def test_runtime_arm_identity_is_stable_until_rearmed(tmp_path):
    rt = runtime(tmp_path, [seen(t) for t in range(4)])
    rt.tick()
    first = rt.armed.arm_id
    assert first
    original_choice = rt._choose
    decision, by, rule = rt.armed.decision, rt.armed.by, rt.armed.rule
    rt._choose = lambda *_: (decision, by, rule, "accepted-agreement")
    rt.tick()
    assert rt.armed.arm_id == first
    assert rt.armed.decision_id == "accepted-agreement"
    rt._choose = original_choice
    rt.finish(SkillOutcome.SUCCEEDED, "observed")
    rt.tick()
    assert rt.armed.arm_id != first
    captured = read(rt.recorder.dir / "ticks.jsonl")
    assert captured[0]["arm_id"] == captured[1]["arm_id"] == first
    assert captured[2]["arm_id"] == rt.armed.arm_id
    assert read(rt.recorder.dir / "skills.jsonl")[0]["arm_id"] == first
    rt.recorder.close()
