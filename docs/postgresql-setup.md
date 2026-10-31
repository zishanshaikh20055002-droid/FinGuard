# PostgreSQL 16 validation setup on Windows

The local `postgresql-x64-16` Windows service is running PostgreSQL **16.15** on
`127.0.0.1:5432`. Authenticated `psql` and `pg_isready` checks succeeded. The dedicated
`finguard_test` and `finguard_migration_test` databases already exist and are owned by
non-superuser roles `fg_test_owner` and `fg_migration_owner`. The restricted normal test role is
`fg_test_runner`. Both databases are at Alembic revision `be2905957c4e`. Revision 0003 adds a
transaction memo and ordered status audit without changing prior financial triggers. Credentials are kept in the local PostgreSQL
passfile and are not stored in Git.

The installation and bootstrap steps below are for a **fresh machine**. Do not recreate the existing
roles or databases on this machine. The procedure is based on the
[PostgreSQL Windows download page](https://www.postgresql.org/download/windows/) and
[EDB's Windows installer guide](https://www.enterprisedb.com/docs/supported-open-source/postgresql/installing/windows).
Docker is optional.

## 1. Install PostgreSQL 16 manually

Choose the **PostgreSQL 16** Windows installer linked from the official PostgreSQL download page.
The EDB installer is tested on 64-bit Windows Server 2019 and 2022 and generally works on comparable
desktop Windows releases. Its [minimum hardware requirements](https://www.enterprisedb.com/docs/supported-open-source/postgresql/installing/requirements/)
are a 1 GHz processor, 2 GB RAM, and 512 MB disk, plus space for data. The graphical installer
requires administrator privileges and current Windows updates.

In **Select Components**, include **PostgreSQL Server** and **Command Line Tools**. The latter
provides `psql`, `pg_isready`, and `createdb`; pgAdmin and Stack Builder are optional for this project.
Use a strong password at the installer prompt. Do not put it in a command argument or in Git. Note
the chosen port (normally 5432). The installer does not normally add its `bin` directory to `PATH`,
so the commands below use the default full path; adjust it if you chose another installation path.

```powershell
Get-Service -Name 'postgresql*'
& 'C:\Program Files\PostgreSQL\16\bin\pg_isready.exe' -h 127.0.0.1 -p 5432
& 'C:\Program Files\PostgreSQL\16\bin\psql.exe' -h 127.0.0.1 -p 5432 -U postgres -d postgres -W -c 'SHOW server_version;'
```

Confirm a PostgreSQL **16.x** version and a running service. `pg_isready` checks the listener;
the `psql` command also checks authentication. Password entry is interactive.

## 2. Inspect before creating dedicated databases

Connect interactively to the administrative `postgres` database with `psql -W`, then inspect names
and owners. Using the installer superuser for this **initial bootstrap only** is acceptable; do not
use it for migrations or application tests.

```sql
SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database
WHERE datname IN ('finguard_test', 'finguard_migration_test');
```

If either name already exists, **stop and inspect** its owner, tables (`\dt public.*` while connected
to that database), and Alembic revision before using it. Never overwrite or drop an existing database.
For absent names, create separate non-superuser roles. Run `\password` interactively for each role,
so passwords do not appear in SQL text or terminal history.

```sql
CREATE ROLE fg_test_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE fg_test_runner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE fg_migration_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
\password fg_test_owner
\password fg_test_runner
\password fg_migration_owner
CREATE DATABASE finguard_test OWNER fg_test_owner;
CREATE DATABASE finguard_migration_test OWNER fg_migration_owner;
```

The owner roles have database-local DDL privileges needed by Alembic; none is a PostgreSQL
superuser. `fg_test_runner` receives only connectivity and table DML for the financial integration
suite. After migrating `finguard_test` as `fg_test_owner`, connect to it as its owner and run:

```sql
GRANT CONNECT ON DATABASE finguard_test TO fg_test_runner;
GRANT USAGE ON SCHEMA public TO fg_test_runner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fg_test_runner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fg_test_runner;
```

## 3. Migrate and inspect

Set `DATABASE_URL` through a local ignored `.env` file or a secure process environment to the
`postgresql+psycopg://` URL for **fg_test_owner@finguard_test**. Do not display the URL. From the
repository root:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Expect revision `be2905957c4e`. In `psql` connected to `finguard_test`, inspect:

```sql
SELECT version_num FROM alembic_version;
\dt public.*
SELECT conrelid::regclass AS table_name, conname, contype
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace
ORDER BY 1, 2;
SELECT tgrelid::regclass AS table_name, tgname
FROM pg_trigger
WHERE NOT tgisinternal AND tgrelid::regclass::text LIKE 'journal_%'
ORDER BY 1, 2;
SELECT proname FROM pg_proc WHERE proname LIKE 'finguard_%' ORDER BY proname;
```

Point `FINGUARD_TEST_DATABASE_URL` at **fg_test_runner@finguard_test** and run
`.\.venv\Scripts\python.exe -m pytest -m integration -q`. The suite checks PostgreSQL 16 and the
head revision. It commits uniquely named test records and does not delete the database.

For the migration cycle, point `FINGUARD_MIGRATION_TEST_DATABASE_URL` and `DATABASE_URL` at
**fg_migration_owner@finguard_migration_test**. The migration test verifies that this database is
owned by the non-superuser role, contains no unrelated tables or application rows, then runs upgrade,
guarded downgrade, downgrade to base, and re-upgrade. Do not run it against a database with user data.

## Downgrade restriction

Revision `20261031_0002` refuses to downgrade if any transaction has `received`, `pending_risk`, or
`challenged` status. It locks and inspects `transactions` before dropping any constraints or triggers
and raises SQLSTATE `P0001`; the transaction and schema remain unchanged. Only an empty table or
rows in `pending`, `approved`, `blocked`, `failed`, or `posted` allow downgrade to `20261031_0001`.
Revision `20261031_0003` additionally refuses to downgrade while `transaction_status_history`
contains any rows; it raises SQLSTATE `P0001` so audit history is not silently discarded. Downgrading
from `cfe7aacc2d92` refuses while any user, session, refresh history, auth audit row, or owned account
exists. Never downgrade the populated `finguard_test` database. Downgrading
from `20261031_0001` to base drops the foundation tables, so perform that cycle only in the
dedicated, inspected `finguard_migration_test` database.
