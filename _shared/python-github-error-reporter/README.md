# python-github-error-reporter

Reports uncaught exceptions from a Python service to GitHub, by sending a
`repository_dispatch` event (`event_type: production-error`) to a repository
you choose. A workflow in that repository, listening for the event, can turn
each one into an issue. Reporting is off unless both a token and a repository
are configured, so a copy of this code never reports anywhere by default.

Standard library only.

## Install

As a path dependency on the copy under `_shared/`, in `requirements.txt`:

```text
./_shared/python-github-error-reporter
```

## Usage

### Register at process startup

```python
from python_github_error_reporter import GitHubErrorReporter

GitHubErrorReporter.register(
    app="my-bridge",
    environment="production",
)
```

That installs `sys.excepthook`, `threading.excepthook`, and (if called
inside an async context) an asyncio loop exception handler. Every
uncaught exception will be dispatched as a production-error event with
`severity=critical`.

For asyncio services that call `asyncio.run(main())`, install the loop
hook inside `main()`:

```python
import asyncio
from python_github_error_reporter import GitHubErrorReporter

async def main():
    GitHubErrorReporter.install_asyncio_hook(asyncio.get_running_loop())
    # ... rest of your bridge ...

asyncio.run(main())
```

### Manual reporting

```python
from python_github_error_reporter import GitHubErrorReporter

try:
    do_thing()
except SomeError as e:
    GitHubErrorReporter.report(
        error_type=type(e).__name__,
        message=str(e),
        severity="high",
        extra="context=do_thing",
    )

# Or from async code:
await GitHubErrorReporter.report_async(
    error_type="DownstreamTimeout",
    message="Upstream did not respond in 30 s",
    severity="critical",
)
```

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `GITHUB_ERROR_TOKEN` | (unset) | GitHub PAT with `repo` scope. Required; reporter is silently disabled when unset. |
| `GITHUB_REPO` | (unset) | Target repo, `owner/name`. Required; reporter is silently disabled when unset. |
| `GITHUB_ERROR_ENVIRONMENT` | `production` | Environment label for the issue's `env:<environment>` label. |

You can also pass these to `register()` explicitly to override the env vars.

## Dedup behavior

- **Reporter side (this lib):** `md5(app + error_type)` cached for 60 s
  (tunable via `deduplication_window` arg to `register()`). Same key within
  the window → silently skipped, never reaches GitHub.
- **Workflow side:** up to the receiving workflow. Treating an existing issue
  with the same title, open or closed, as a no-op means each unique error
  opens one issue, however many times it recurs.

## Wire payload

What this reporter sends, as the body of the `repository_dispatch` request:

```json
{
  "event_type": "production-error",
  "client_payload": {
    "app": "...",
    "error_type": "...",
    "message": "... (capped at 5000 chars)",
    "stacktrace": "... (capped at 10000 chars)",
    "severity": "critical|high|medium|low",
    "environment": "production|...",
    "timestamp": "2026-05-24T18:36:47Z",
    "url": "",
    "extra": ""
  }
}
```

## Tests

From this directory:

```bash
python3 -m pytest tests/ -v
```
