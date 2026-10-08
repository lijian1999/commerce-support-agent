"""评测运行器的单元测试：不叫模型，跑通用的假事件流；涉及数据库的测试连真实库。"""
import pytest

from app.context import AppContext
from app.db import connect
from evals import runner
from evals.schema import POISONED_INDEX_DIR, Case


def make_case(**kw) -> Case:
    return Case.model_validate(
        {"id": "c1", "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}], **kw}
    )


def _final(**values) -> tuple:
    return ("final", values)


# ---- 会话编号 ----

def test_session_id() -> None:
    assert runner.session_id(make_case(), 2) == "eval-c1-2"


# ---- 单条 Case 的运行 ----

def test_run_case_records_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_turn(graph, ctx, thread, text, resume):
        yield ("step", {"name": "router", "detail": "路由"})
        yield ("step", {"name": "answer", "detail": "回答"})
        yield _final(route="direct", answer="您好", sources=None, task_results=None)

    monkeypatch.setattr(runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(runner, "make_context", lambda *a, **kw: AppContext())
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)

    result = runner.run_case(make_case(), 0, "h" * 64)
    assert result["case_id"] == "c1"
    assert result["run_index"] == 0
    assert result["case_hash"] == "h" * 64
    assert len(result["turns"]) == 1
    turn = result["turns"][0]
    assert turn["question"] == "你好"
    assert turn["route"] == "direct"
    assert turn["answer"] == "您好"
    assert turn["sources"] == []
    assert turn["task_results"] == {}
    assert turn["error"] is None
    assert turn["db_orders"] == {}
    assert [step["name"] for step in turn["steps"]] == ["router", "answer"]


def test_run_case_resumes_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_run_turn(graph, ctx, thread, text, resume):
        calls.append({"text": text, "resume": resume})
        if len(calls) == 1:
            yield ("interrupt", {"order_id": "ORD-1"})
        else:
            yield _final(route="cancel")

    monkeypatch.setattr(runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(runner, "make_context", lambda *a, **kw: AppContext())
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)

    case = make_case(turns=[{"user": "帮我取消订单", "confirm": True}])
    result = runner.run_case(case)

    assert len(calls) == 2
    assert calls[1]["text"] is None
    assert calls[1]["resume"] == {"confirm": True, "order_id": "ORD-1"}
    assert result["turns"][0]["interrupts"] == [{"order_id": "ORD-1"}]


def test_run_case_captures_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_run_turn(graph, ctx, thread, text, resume):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        yield _final(route="direct")

    monkeypatch.setattr(runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(runner, "make_context", lambda *a, **kw: AppContext())
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)

    result = runner.run_case(make_case(turns=[{"user": "第一问"}, {"user": "第二问"}]))

    assert len(result["turns"]) == 2
    assert result["turns"][0]["error"] == "RuntimeError: boom"
    assert result["turns"][0]["route"] is None
    assert result["turns"][1]["error"] is None
    assert result["turns"][1]["route"] == "direct"


def test_run_case_poisoned_index_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def fake_make_context(customer_id, today=None, fault="off", session_id="",
                          index_version=None, index_dir=None):
        seen.append(index_dir)
        return AppContext()

    monkeypatch.setattr(runner, "run_turn", lambda *a: iter([_final(route="direct")]))
    monkeypatch.setattr(runner, "make_context", fake_make_context)
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)

    runner.run_case(make_case(env={"index": "poisoned"}))
    runner.run_case(make_case())
    assert seen == [str(POISONED_INDEX_DIR), None]


# ---- 数据库相关的函数 ----

def _set_cancelled(order_id: str) -> None:
    """把订单改成已取消并留一条取消记录，模拟取消成功的结果。"""
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = %s, cancelled_at = %s WHERE order_id = %s",
                        ["Cancelled", "2025-10-20 10:00:00", order_id])
            cur.execute(
                "INSERT INTO order_cancellations (order_id, customer_id, session_id, trace_id, cancelled_at) "
                "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                "customer_id = VALUES(customer_id), session_id = VALUES(session_id), "
                "trace_id = VALUES(trace_id), cancelled_at = VALUES(cancelled_at)",
                [order_id, "CUST-001", "eval-t-0", "trace-t", "2025-10-20 10:00:00"],
            )
        conn.commit()
    finally:
        conn.close()


def _order_row(order_id: str) -> dict:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, cancelled_at FROM orders WHERE order_id = %s", [order_id])
            row = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS n FROM order_cancellations WHERE order_id = %s", [order_id])
            count = cur.fetchone()["n"]
    finally:
        conn.close()
    return {"status": row["status"], "cancelled_at": row["cancelled_at"], "cancellations": count}


def test_run_case_reads_db_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "run_turn", lambda *a: iter([_final(route="sql")]))
    monkeypatch.setattr(runner, "make_context", lambda *a, **kw: AppContext())
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)

    case = make_case(turns=[{"user": "查订单",
                             "expect": {"db_orders": {"ORD-2025-0031": "Processing",
                                                      "ORD-0000-0000": "Processing"}}}])
    result = runner.run_case(case)

    assert result["turns"][0]["db_orders"] == {"ORD-2025-0031": "Processing"}


def test_restore_orders() -> None:
    _set_cancelled("ORD-2025-0038")

    runner.restore_orders(["ORD-2025-0038"])

    row = _order_row("ORD-2025-0038")
    assert row["status"] == "Processing"
    assert row["cancelled_at"] is None
    assert row["cancellations"] == 0


def test_run_case_restores_even_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_turn(graph, ctx, thread, text, resume):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(runner, "make_context", lambda *a, **kw: AppContext())
    monkeypatch.setattr(runner, "build_graph", lambda **kw: None)
    _set_cancelled("ORD-2025-0038")

    result = runner.run_case(make_case(restore=["ORD-2025-0038"]))

    assert result["turns"][0]["error"] == "RuntimeError: boom"
    assert _order_row("ORD-2025-0038")["status"] == "Processing"


def test_cleanup_sessions() -> None:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO handoff_tickets (ticket_id, customer_id, reason, summary, order_ids, "
                "session_id, trace_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                ["TK-DEL-1", "CUST-001", "user_request", "评测", "", "eval-t-0", "trace-t",
                 "2025-10-20 10:00:00"],
            )
            cur.execute(
                "INSERT INTO handoff_tickets (ticket_id, customer_id, reason, summary, order_ids, "
                "session_id, trace_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                ["TK-KEEP-1", "CUST-001", "user_request", "评测", "", "s-keep", "trace-t",
                 "2025-10-20 10:00:00"],
            )
        conn.commit()
    finally:
        conn.close()

    try:
        assert runner.cleanup_sessions()["tickets"] == 1

        conn = connect("root")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM handoff_tickets WHERE ticket_id = %s", ["TK-KEEP-1"])
                assert cur.fetchone() == {"session_id": "s-keep"}
        finally:
            conn.close()
    finally:
        conn = connect("root")
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM handoff_tickets WHERE ticket_id = %s", ["TK-KEEP-1"])
            conn.commit()
        finally:
            conn.close()


# ---- 串行判断和多条 Case 的运行 ----

def test_needs_serial() -> None:
    assert runner.needs_serial(make_case()) is False
    assert runner.needs_serial(make_case(restore=["ORD-1"])) is True
    assert runner.needs_serial(make_case(turns=[{"user": "你好", "confirm": True}])) is True
    assert runner.needs_serial(make_case(
        turns=[{"user": "你好", "expect": {"db_orders": {"ORD-1": "Processing"}}}])) is True


def test_run_cases_order_and_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def fake_run_case(case: Case, run_index: int = 0, case_hash: str = "") -> dict:
        return {"case_id": case.id, "case_hash": case_hash, "run_index": run_index, "turns": []}

    class FakeStore:
        def __init__(self, index_dir=None) -> None:
            pass

    cleanup_count = [0]

    def fake_cleanup() -> dict:
        cleanup_count[0] += 1
        return {"cancellations": 0, "tickets": 0}

    monkeypatch.setattr(runner, "run_case", fake_run_case)
    monkeypatch.setattr(runner, "IndexStore", FakeStore)
    monkeypatch.setattr(runner, "cleanup_sessions", fake_cleanup)

    cases = [
        Case.model_validate({"id": "b", "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}]}),
        Case.model_validate({"id": "a", "business": "闲聊", "situation": "x",
                             "restore": ["ORD-1"], "turns": [{"user": "你好"}]}),
    ]
    runs = runner.run_cases(cases, repeats=2, hashes={"a": "ha"},
                            progress=lambda done, total, run: calls.append((done, total, run)))

    assert len(runs) == 4
    assert [(run["case_id"], run["run_index"]) for run in runs] == [
        ("a", 0), ("a", 1), ("b", 0), ("b", 1)]
    assert {run["case_hash"] for run in runs if run["case_id"] == "a"} == {"ha"}
    assert {run["case_hash"] for run in runs if run["case_id"] == "b"} == {""}
    assert len(calls) == 4
    assert calls[-1][0] == 4 and calls[-1][1] == 4
    assert cleanup_count[0] == 1


def test_run_cases_requires_poisoned_index(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStore:
        def __init__(self, index_dir=None) -> None:
            pass

        def read_pointer(self):
            return None

    monkeypatch.setattr(runner, "IndexStore", FakeStore)
    cases = [Case.model_validate({"id": "p", "business": "知识问答", "situation": "x",
                                  "env": {"index": "poisoned"}, "turns": [{"user": "你好"}]})]

    with pytest.raises(RuntimeError) as exc:
        runner.run_cases(cases, repeats=1)
    assert "evals.poisoned_index" in str(exc.value)
