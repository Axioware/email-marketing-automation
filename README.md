# Email Marketing Automation Database

## Run migrations

Install dependencies, set `DATABASE_URL` in `.env` or the environment, then apply the schema:

```sh
python -m pip install -r requirements.txt
alembic upgrade head
```

On another machine, provide a `DATABASE_URL` that points to its PostgreSQL server. The migration files are committed with the project; database credentials should remain in `.env` or a deployment secret and must not be committed.

To inspect the current migration revision, run `alembic current`. To roll back this initial schema, run `alembic downgrade base`.