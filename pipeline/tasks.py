"""Background batches on Vercel. Vercel Queues runs `advance_run` in its own function invocation (the subscriber in
vercel_worker.py, declared in pyproject.toml); each call runs one batch of a web run (pipeline/webruns.py) and queues
the next. The `vercel://` broker exists only on Vercel; elsewhere runs never go through here.
"""
from celery import Celery
from django.db import close_old_connections, connections

app = Celery("outreach", broker="vercel://")
app.conf.update(
    task_acks_late=True,  # a batch that dies (timeout, crash) is delivered again once its lease expires
    task_ignore_result=True,
    broker_transport_options={"lease_duration": 360},  # longer than a batch (webruns.LEASE)
)


@app.task(name="pipeline.advance_run")
def advance_run(run_id: int) -> None:
    from pipeline import webruns

    close_old_connections()
    try:
        if webruns.advance(run_id):
            webruns.kick(run_id)
    finally:
        connections.close_all()  # nothing closes them between queue deliveries
