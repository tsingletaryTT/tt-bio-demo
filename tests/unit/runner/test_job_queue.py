from runner.queue import Job, JobQueue


def _job(name, priority=0):
    return Job(job_id=name, target_id=name, input_path=f"/tmp/{name}.yaml",
               priority=priority)


def test_an_empty_queue_hands_back_nothing():
    assert JobQueue().take() is None


def test_jobs_of_equal_priority_come_out_in_submission_order():
    q = JobQueue()
    for name in ("a", "b", "c"):
        q.submit(_job(name))
    assert [q.take().job_id for _ in range(3)] == ["a", "b", "c"]


def test_a_higher_priority_job_jumps_the_queue():
    q = JobQueue()
    q.submit(_job("attract-1"))
    q.submit(_job("attract-2"))
    q.submit(_job("visitor", priority=10))
    assert q.take().job_id == "visitor"


def test_a_late_visitor_pick_still_jumps_ahead_of_older_attract_jobs():
    q = JobQueue()
    q.submit(_job("attract-1"))
    q.submit(_job("visitor-1", priority=10))
    q.submit(_job("attract-2"))
    q.submit(_job("visitor-2", priority=10))
    assert [q.take().job_id for _ in range(4)] == [
        "visitor-1", "visitor-2", "attract-1", "attract-2"]


def test_length_and_pending_reflect_what_is_waiting():
    q = JobQueue()
    assert len(q) == 0
    q.submit(_job("a"))
    q.submit(_job("b"))
    assert len(q) == 2
    assert [j.job_id for j in q.pending] == ["a", "b"]
    q.take()
    assert len(q) == 1


def test_pending_is_a_snapshot_that_cannot_mutate_the_queue():
    q = JobQueue()
    q.submit(_job("a"))
    q.pending.clear()
    assert len(q) == 1


def test_the_queue_is_safe_to_use_from_two_threads():
    import threading
    q = JobQueue()
    for i in range(200):
        q.submit(_job(f"j{i}"))
    taken = []
    lock = threading.Lock()

    def drain():
        while True:
            job = q.take()
            if job is None:
                return
            with lock:
                taken.append(job.job_id)

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(taken) == 200
    assert len(set(taken)) == 200, "a job was handed out twice"
