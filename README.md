# django-llms-manager

[![Checks](https://github.com/KitKat-ru/django-llms-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/KitKat-ru/django-llms-manager/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Manage `llms.txt` and `llms-full.txt` content for AI assistants through the Django
admin. Edit Markdown or import a UTF-8 file, save revisions, review changes, and
publish a selected version at the site root or in a nested directory.

Requires Python 3.10+ and Django 4.2+. Supported Python/Django combinations follow
[Django's compatibility table](https://docs.djangoproject.com/en/stable/faq/install/#what-python-version-can-i-use-with-django).

## Setup

To use a local checkout in an existing Django project managed with uv, add it as a
[path dependency](https://docs.astral.sh/uv/concepts/projects/dependencies/#path):

```bash
uv add /path/to/django-llms-manager
```

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

Apply the included migrations using the host project's settings:

```bash
uv run python manage.py migrate
```

Open **llms** in the Django admin to create named documents. Each document has its
own editor and revision history. Open **Publications** to configure their public
addresses and select the revisions served there.

## Revisions and publishing

1. Create or select a document and open its **Editor** or **Revision history**.
2. Edit the complete Markdown document, or use **Load file into editor** to import
   a UTF-8 file. Importing replaces the editor contents without saving a revision.
3. **Save revision** creates an immutable file snapshot belonging to that document.
   Revisions are numbered from 1 independently within each document.
4. Create or open a **Publication**. Its address consists of a directory and a file
   name selected from `llms.txt` and `llms-full.txt`.
5. In the publication card, select a document and click **Show revisions**. Select
   any of its saved revisions and click **Review revision**. Selection and review
   are available before saving a new publication; the entered address is preserved.
6. Review the content and diff against the revision currently published at this
   particular address. If the address is new or has changed, click **Save address**;
   the selected document and revision are kept. Then click **Publish revision**.
   Both buttons are at the top of the card. Publishing becomes available after
   reviewing a revision and saving the address. **Save address** only saves the
   address; changing the selection or reviewing a diff does not publish anything.

When the selected revision is already published at this address, the card shows
its status, complete content, and public link. The diff is hidden.

| Directory | File name | Public address |
| --- | --- | --- |
| `/` | `llms.txt` | `/llms.txt` |
| `/` | `llms-full.txt` | `/llms-full.txt` |
| `/courses/python/` | `llms.txt` | `/courses/python/llms.txt` |
| `/courses/python/` | `llms-full.txt` | `/courses/python/llms-full.txt` |

Each directory/file-name pair is unique. Directory input is normalized: for
example, `courses/python` becomes `/courses/python/`. Editing a saved directory or
file name changes the public URL while keeping its selected revision.

Publication changes are independent for each address. If its selected revision
has changed since the review, publication is rejected and the updated diff is
shown. Updating another address does not invalidate the review.

The revision history records the author and creation date. Use **Edit as new
revision** to continue working from a saved snapshot. To restore an older version,
select it in the publication card, review its diff, and publish it again. Any saved
revision can be selected, including an older revision of a different document.
Publication actions are recorded in the publication's Django admin **History**.

Public files are available without logging in and return the selected content as
`text/plain; charset=utf-8`. The publication view accepts `GET` and `HEAD` and only
reads existing publications; requesting a URL does not create documents, revisions,
or publications.
A configured address without a selected revision returns an empty response with
status `200`. An unconfigured address returns `404`. The routes match only the
two supported file names, at the root or in nested directories.
Markdown is displayed as text; the application does not enforce a section schema.
Links between root and nested files belong to the Markdown content. The library
does not automatically assemble `llms-full.txt` from other publications.

The `view_document` permission allows viewing document history and previews.
The `add_document` permission allows creating documents. The `change_document`
permission allows renaming documents and importing and saving revisions.

The `view_publication` permission allows viewing publication cards and history.
The `add_publication` permission allows configuring new addresses, and
`change_publication` allows changing addresses and confirming publications.
Selecting and publishing document revisions also requires `view_document` or
`change_document`. Removing an address through the standard `delete_publication`
permission does not delete its document or revision files.
Documents and revisions cannot be deleted through the library's admin interface.

Each `Publication` points to one selected revision; its document is determined
through `Revision.document`. The same revision can be served at multiple addresses.
Changing a publication preserves all documents, their revisions, and their files.
Address uniqueness uses a normal constraint on directory and file name, without
conditional indexes.

## File storage

Revision files use Django's default storage, selected by `STORAGES["default"]`.
The database stores file names and revision metadata. Each snapshot has a unique
key such as `llms_manager/<uuid>.txt`.

Public addresses and storage keys are independent. For example,
`/courses/python/llms-full.txt` serves the file referenced by that publication's
revision. The directory and public file name do not determine the storage key.
Each revision gets a new key, so saving another revision preserves earlier files.

For local storage, configure `MEDIA_ROOT`, for example:

```python
MEDIA_ROOT = BASE_DIR / "media"
```

For S3, install `django-storages[s3]` in the host project and configure its
[S3 backend](https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html),
for example:

```python
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"bucket_name": "my-bucket"},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
```

Credentials and other backend options belong to the host project's configuration.
The library reads files through the Storage API and serves their content at the
configured public endpoint. Other file fields using the default storage share
this backend. Access to storage objects themselves follows the project's storage
configuration.

Database and file storage are separate systems. Keep both when backing up or
moving the application. When changing storage backends, copy existing revision
files while preserving the keys stored in the database. A database rollback in
an enclosing transaction can leave an unreferenced file in storage.

## Caching

Public file content uses [Django's default cache](https://docs.djangoproject.com/en/4.2/topics/cache/#accessing-the-cache)
from `CACHES`. Set the lifetime in the host project's settings:

```python
LLMS_MANAGER_CACHE_TIMEOUT = 60  # Seconds; 0 or None disables caching.
```

If the setting is absent, the default is 60 seconds. Set `0` or `None` to disable
caching. Positive values must be integers. Invalid values, including strings,
booleans, floats, and negative integers, disable caching and emit a `RuntimeWarning`
when a published revision is requested. Repeated warnings follow the project's
[Python warning filters](https://docs.python.org/3/library/warnings.html#the-warnings-filter).

Each request looks up the publication in the database to determine its current
revision. The revision's text is read from the cache,
or loaded from Storage and cached on a miss. Publishing another revision selects
a different cache key, so the next request serves the newly selected content.
Restoring an earlier revision can reuse its cached text until that entry expires.
The same revision can share its cached text across multiple public addresses.

This caches revision text on the server; it does not add HTTP cache headers.
Address changes and deletions take effect on the next request. Admin previews
continue to read files directly from Storage.

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
`/llms.txt` returns `404`. After saving its address, it returns an empty file until
a revision is published. Local revision files are stored in `media/llms_manager/`.
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

English (`en`) and Russian (`ru`) catalogs are stored in `llms_manager/locale/`.
The interface uses the active Django language. To use Russian by default, set
`LANGUAGE_CODE = "ru"` in the host project with `USE_I18N = True`.

To update the catalogs, run these commands with GNU gettext installed:

```bash
cd llms_manager
uv run python -m django makemessages -l en -l ru
```

Edit `msgstr` values in `locale/*/LC_MESSAGES/django.po`. Review entries marked
`fuzzy` and remove that flag after correcting their translations.
[Django skips fuzzy entries during compilation](https://docs.djangoproject.com/en/stable/topics/i18n/translation/#message-files).
Some Russian entries still need translation or review and fall back to English.

Then compile the catalogs from the same directory:

```bash
uv run python -m django compilemessages -l en -l ru
```

Keep the `.po` sources and compiled `.mo` files together in version control.
Both are included in the distributed package.

## Building

```bash
uv build
uv run twine check dist/*
```

The `uv_build` backend produces a wheel and source distribution in `dist/`,
including the MIT license from `LICENSE`. The `llms_manager` package includes
migrations, admin templates, static assets, and translation catalogs. With the
current [build configuration](https://docs.astral.sh/uv/concepts/build-backend/#file-inclusion-and-exclusion),
`tests/`, `.python-version`, `db.sqlite3`, and uploaded media are excluded from both
distributions.

## Releasing

Before the first release:

1. Create GitHub environments named `testpypi` and `pypi`.
2. Configure a trusted publisher on both
   [TestPyPI](https://test.pypi.org/manage/account/publishing/) and
   [PyPI](https://pypi.org/manage/account/publishing/), using these values:

   | Field | Value |
   | --- | --- |
   | Project name | `django-llms-manager` |
   | Owner | `KitKat-ru` |
   | Repository | `django-llms-manager` |
   | Workflow filename | `release.yml` |
   | Environment | `testpypi` on TestPyPI, `pypi` on PyPI |

For each release, update `version` in `pyproject.toml`, commit the changes, and
push a tag named `v<version>` (for example, `v0.1.0`).

The Release workflow checks that the tag matches the package version and runs
the Checks workflow. After the checks pass, it publishes the same distributions
to TestPyPI and then PyPI through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
