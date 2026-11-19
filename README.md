# django-llms-manager

[![Tests](https://github.com/KitKat-ru/django-llms-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/KitKat-ru/django-llms-manager/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/django-llms-manager)](https://pypi.org/project/django-llms-manager/)
[![Downloads](https://img.shields.io/pypi/dm/django-llms-manager)](https://pypistats.org/packages/django-llms-manager)
[![Python](https://img.shields.io/pypi/pyversions/django-llms-manager)](https://pypi.org/project/django-llms-manager/)
[![Django](https://img.shields.io/pypi/frameworkversions/django/django-llms-manager)](https://pypi.org/project/django-llms-manager/)
[![License](https://img.shields.io/badge/license-MIT-blue)](https://github.com/KitKat-ru/django-llms-manager/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)

Manage `llms.txt` and `llms-full.txt` content for AI assistants through the Django
admin. Edit Markdown or import a UTF-8 file, save revisions, review changes, and
publish a selected version at the site root or in a nested directory.

Requires Python 3.10+ and Django 4.2+. Supported Python/Django combinations follow
[Django's compatibility table](https://docs.djangoproject.com/en/stable/faq/install/#what-python-version-can-i-use-with-django).

CI is configured to check all 22 officially supported combinations of the declared
Python and Django series, using the latest available patch release of each Django
series:

| Python | Django series |
| --- | --- |
| 3.10 | 4.2, 5.0, 5.1, 5.2 |
| 3.11 | 4.2, 5.0, 5.1, 5.2 |
| 3.12 | 4.2, 5.0, 5.1, 5.2, 6.0, 6.1 |
| 3.13 | 5.1, 5.2, 6.0, 6.1 |
| 3.14 | 5.2, 6.0, 6.1 |
| 3.15 | 6.1 |

## Setup

Install the package with your preferred package manager:

- pip: `python -m pip install django-llms-manager`
- Poetry: `poetry add django-llms-manager`
- uv: `uv add django-llms-manager`

Add the application to a Django project with the admin enabled:

```python
INSTALLED_APPS = [
    ...,
    "llms_manager",
]
```

Mount the publication routes at the site root, after specific application routes
and before any catch-all route:

```python
from django.urls import include, path

urlpatterns = [
    ...,
    path("", include("llms_manager.urls")),
]
```

Apply the included migrations using the host project's settings. Run Python in
that project's environment, or use `poetry run python` / `uv run python`:

```bash
python manage.py migrate
```

Open **Documents** in the Django admin to create named documents. Each document has its
own editor and revision history. Open **Publications** to configure their public
addresses and select the revisions served there.

## Revisions and publishing

1. Create a document in **Documents**, open its **Editor**, and edit Markdown or
   import a UTF-8 file. Click **Save revision**.
2. Create or open a publication in **Publications** and set the directory and
   file name (`llms.txt` or `llms-full.txt`).
3. Select a document, click **Show revisions**, choose a revision, and click
   **Review revision**.
4. Click **Save and publish** to save the address and selected revision together.

Saving a publication requires a reviewed revision and publishes it immediately.

| Directory | File name | Public address |
| --- | --- | --- |
| `/` | `llms.txt` | `/llms.txt` |
| `/courses/python/` | `llms-full.txt` | `/courses/python/llms-full.txt` |

Each directory/file-name pair is unique. Changing the address moves the publication.
Both file names are managed independently; `llms-full.txt` is not assembled
automatically.

Revisions are immutable. To restore earlier content, review and publish an older
revision. If another editor publishes first, review the updated diff before retrying.

Published content is publicly available as UTF-8 plain text. The admin uses Django's
model permissions; selecting and publishing revisions also requires `view_document`
or `change_document`. Documents and revisions cannot be deleted through the admin.

## File storage

Revision files use Django's default storage (`STORAGES["default"]`). For local files:

```python
MEDIA_ROOT = BASE_DIR / "media"
```

For S3, configure the
[django-storages S3 backend](https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html)
in the host project.

The database stores revision metadata and file keys such as
`llms_manager/<uuid>.txt`. Back up both the database and revision files. When moving
to another storage backend, preserve the stored file keys.

## Caching

Revision text uses Django's default cache. Configure the lifetime in seconds
(default: 60):

```python
LLMS_MANAGER_CACHE_TIMEOUT = 60
```

Use a non-negative integer or `None`. Both `0` and `None` disable caching.

Each request resolves the current publication, so a newly published revision is
served on the next request. Admin previews read directly from storage.

## Development

Development uses Python 3.13 and [uv](https://docs.astral.sh/uv/). Run these commands
from the repository root to prepare the environment:

```bash
uv sync
uv run pre-commit install
```

The `.python-version` file selects Python 3.13 for development; package support
starts at Python 3.10 as declared in `pyproject.toml`.
[`uv sync`](https://docs.astral.sh/uv/concepts/projects/sync/) synchronizes `.venv/`
with the project dependencies and `uv.lock`. The `dev` dependency group includes
the `test` group with pytest and pytest-django. Pytest uses `tests.settings`.

`tests/` contains a minimal Django project with the admin enabled and a SQLite
database stored in `db.sqlite3`. It is not part of the distributed package.

To run the project locally:

```bash
export DJANGO_SETTINGS_MODULE=tests.settings
uv run python -m django migrate
uv run python -m django createsuperuser
uv run python -m django runserver
```

Open [the admin](http://127.0.0.1:8000/admin/) or
[llms.txt](http://127.0.0.1:8000/llms.txt). Before a root publication is created,
`/llms.txt` returns `404`. Creating a publication through the admin immediately
serves its selected revision. Local revision files are stored in `media/llms_manager/`.
The development project defaults to Russian (`LANGUAGE_CODE = "ru"` in
`tests/settings.py`); set it to `"en"` for English.

Run the checks from the repository root:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m django check --settings=tests.settings
uv run python -m django makemigrations llms_manager --check --dry-run --settings=tests.settings
uv run pytest
```

After model changes, generate and review migrations with
`uv run python -m django makemigrations llms_manager --settings=tests.settings`.
Then apply them with `uv run python -m django migrate --settings=tests.settings`.
The initial setup uses the included migrations.

Ruff handles linting, import sorting, and formatting through pre-commit and targets
Python 3.10 syntax. To run all hooks, including automatic fixes and formatting:

```bash
uv run pre-commit run --all-files
```

Pre-commit checks files tracked by Git. The direct Ruff commands above also cover
new Python files before they are tracked.

CI builds the distributions, checks their metadata, installs the wheel with each
Python/Django pair in the matrix, checks for missing migrations, and runs the tests
from outside the source directory.

## Translations

English (`en`) and Russian (`ru`) catalogs are in `llms_manager/locale/`. The
interface uses the active Django language. For Russian by default, set
`LANGUAGE_CODE = "ru"` and `USE_I18N = True` in the host project.

With GNU gettext installed, update the catalogs:

```bash
cd llms_manager
uv run python -m django makemessages -l en -l ru
```

Edit `msgstr` values in `locale/*/LC_MESSAGES/django.po`, then compile:

```bash
uv run python -m django compilemessages -l en -l ru
```

Keep both `.po` and compiled `.mo` files in version control.
