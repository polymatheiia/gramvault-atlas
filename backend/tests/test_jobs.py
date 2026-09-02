"""Background job lifecycle (`gramvault.api.jobs`): create/conflict,
run -> done/failed/cancelled, progress, cooperative cancel, orphan
reclaim.
"""

from __future__ import annotations

import sqlite3

import pytest

from gramvault.api import jobs
from gramvault.config import Config
from gramvault.db.session import session_scope
from gramvault.models.schemas import JobKind, JobStatus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestCreate:
    def test_creates_a_pending_job_with_params(self, tmp_db_conn: sqlite3.Connection) -> None:
        job = jobs.create(tmp_db_conn, JobKind.ENRICH, params={"item_ids": [1, 2]})
        assert job.id is not None
        assert job.kind == JobKind.ENRICH
        assert job.status == JobStatus.PENDING
        assert job.params == {"item_ids": [1, 2]}

    def test_second_active_job_of_same_kind_conflicts(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        jobs.create(tmp_db_conn, JobKind.ENRICH)
        with pytest.raises(jobs.JobConflict):
            jobs.create(tmp_db_conn, JobKind.ENRICH)

    def test_different_kinds_do_not_conflict(self, tmp_db_conn: sqlite3.Connection) -> None:
        jobs.create(tmp_db_conn, JobKind.ENRICH)
        jobs.create(tmp_db_conn, JobKind.CATEGORIZE)  # no raise

    def test_new_job_allowed_once_previous_finished(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        first = jobs.create(tmp_db_conn, JobKind.ENRICH)
        tmp_db_conn.execute(
            "UPDATE jobs SET status = 'done' WHERE id = ?", (first.id,)
        )
        tmp_db_conn.commit()
        jobs.create(tmp_db_conn, JobKind.ENRICH)  # no raise

    def test_conflict_does_not_leave_a_half_written_row(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        jobs.create(tmp_db_conn, JobKind.ENRICH)
        with pytest.raises(jobs.JobConflict):
            jobs.create(tmp_db_conn, JobKind.ENRICH)
        assert (
            tmp_db_conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        )


class TestRun:
    @pytest.mark.anyio
    async def test_success_marks_done_and_stores_result(self, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            job = jobs.create(conn, JobKind.ENRICH)

        async def work(ctx: jobs.JobContext) -> dict:
            ctx.progress(done=3, total=3)
            return {"processed": 3}

        await jobs.run(job.id, work, tmp_config)

        with session_scope(tmp_config) as conn:
            done = jobs.get(conn, job.id)
        assert done.status == JobStatus.DONE
        assert done.result == {"processed": 3}
        assert done.progress == {"done": 3, "total": 3}
        assert done.started_at is not None
        assert done.finished_at is not None

    @pytest.mark.anyio
    async def test_exception_marks_failed_with_message(self, tmp_config: Config) -> None:
        with session_scope(tmp_config) as conn:
            job = jobs.create(conn, JobKind.ENRICH)

        async def work(_: jobs.JobContext) -> dict:
            raise RuntimeError("boom")

        await jobs.run(job.id, work, tmp_config)  # must not raise

        with session_scope(tmp_config) as conn:
            failed = jobs.get(conn, job.id)
        assert failed.status == JobStatus.FAILED
        assert "boom" in failed.error_message

    @pytest.mark.anyio
    async def test_cancel_request_is_seen_mid_run_and_marks_cancelled(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            job = jobs.create(conn, JobKind.ENRICH)

        seen: list[bool] = []

        async def work(ctx: jobs.JobContext) -> dict:
            seen.append(ctx.cancelled)  # False
            with session_scope(tmp_config) as conn:
                jobs.request_cancel(conn, job.id)
            seen.append(ctx.cancelled)  # True — re-read from DB
            return {}

        await jobs.run(job.id, work, tmp_config)

        assert seen == [False, True]
        with session_scope(tmp_config) as conn:
            assert jobs.get(conn, job.id).status == JobStatus.CANCELLED

    @pytest.mark.anyio
    async def test_frees_the_active_slot_so_a_new_job_can_start(
        self, tmp_config: Config
    ) -> None:
        with session_scope(tmp_config) as conn:
            job = jobs.create(conn, JobKind.ENRICH)

        async def work(_: jobs.JobContext) -> dict:
            return {}

        await jobs.run(job.id, work, tmp_config)

        with session_scope(tmp_config) as conn:
            jobs.create(conn, JobKind.ENRICH)  # no conflict


class TestCancelAndReclaim:
    def test_request_cancel_on_missing_job_raises(self, tmp_db_conn: sqlite3.Connection) -> None:
        with pytest.raises(jobs.JobNotFound):
            jobs.request_cancel(tmp_db_conn, 424242)

    def test_request_cancel_on_finished_job_is_a_noop(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        job = jobs.create(tmp_db_conn, JobKind.ENRICH)
        tmp_db_conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job.id,))
        tmp_db_conn.commit()
        result = jobs.request_cancel(tmp_db_conn, job.id)
        assert result.status == JobStatus.DONE
        assert result.cancel_requested is False

    def test_reclaim_orphans_fails_active_jobs(self, tmp_db_conn: sqlite3.Connection) -> None:
        a = jobs.create(tmp_db_conn, JobKind.ENRICH)
        b = jobs.create(tmp_db_conn, JobKind.DIGEST)
        tmp_db_conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (b.id,))
        tmp_db_conn.execute(
            "INSERT INTO jobs (kind, status) VALUES ('pull', 'done')"
        )
        tmp_db_conn.commit()

        reclaimed = jobs.reclaim_orphans(tmp_db_conn)
        tmp_db_conn.commit()

        assert reclaimed == 2
        assert jobs.get(tmp_db_conn, a.id).status == JobStatus.FAILED
        assert jobs.get(tmp_db_conn, b.id).status == JobStatus.FAILED
        assert "restart" in jobs.get(tmp_db_conn, a.id).error_message


class TestListing:
    def test_list_filters_by_kind_and_status_newest_first(
        self, tmp_db_conn: sqlite3.Connection
    ) -> None:
        j1 = jobs.create(tmp_db_conn, JobKind.ENRICH)
        tmp_db_conn.execute("UPDATE jobs SET status='done' WHERE id=?", (j1.id,))
        j2 = jobs.create(tmp_db_conn, JobKind.ENRICH)
        jobs.create(tmp_db_conn, JobKind.DIGEST)
        tmp_db_conn.commit()

        enrich = jobs.list_jobs(tmp_db_conn, kind=JobKind.ENRICH)
        assert [j.id for j in enrich] == [j2.id, j1.id]

        pending = jobs.list_jobs(tmp_db_conn, status=JobStatus.PENDING)
        assert {j.kind for j in pending} == {JobKind.ENRICH, JobKind.DIGEST}
