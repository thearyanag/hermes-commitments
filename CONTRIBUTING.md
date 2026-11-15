# Development

The repository root is the installable plugin. Keep runtime changes independent
of Hermes core patches. Use the public Python plugin APIs and Desktop SDK.
There are no additional runtime Python dependencies and no JavaScript build step.

## Quick behavior checks

From the repository root with Python 3.14 and Node 26:

```sh
python3 -m unittest discover -s tests -v
node --test tests/desktop_bridge.test.mjs
```

Without Hermes on `PYTHONPATH`, the real-host integration tests skip. That quick
run is not a release validation. No test requires provider credentials; the
behavior suites use synthetic conversations and temporary SQLite databases.

## Full pinned checks

The workflow in `.github/workflows/ci.yml` pins Hermes to v0.23.1 commit
`3b421578588243f920886e6fe79a6faa87ea026e`, Python 3.14.9, Node 26.12.1, uv
0.13.5, and Ruff 0.15.22. From a new checkout, prepare Hermes in an isolated
directory (these commands download source and dependencies):

```sh
git clone https://github.com/NousResearch/hermes-agent.git .ci-hermes
git -C .ci-hermes checkout --detach 3b421578588243f920886e6fe79a6faa87ea026e
uv sync --project .ci-hermes --frozen --no-dev --python 3.14.9
python3 scripts/package_release.py
```

Then run the exported tree, using the pinned environment. The following is Bash:

```bash
RELEASE_ROOT="$PWD"
export PYTHONPATH="$RELEASE_ROOT/dist/release/source:$RELEASE_ROOT/.ci-hermes"
export HERMES_HOME="$(mktemp -d)"
export PYTHONDONTWRITEBYTECODE=1
HERMES_PYTHON="$RELEASE_ROOT/.ci-hermes/.venv/bin/python"
cd dist/release/source
"$HERMES_PYTHON" -c 'from hermes_state import SessionDB; assert SessionDB'
"$HERMES_PYTHON" -m unittest discover -s tests -v
node --test tests/desktop_bridge.test.mjs
uv tool run --isolated --from ruff==0.15.22 ruff check *.py tests scripts
uv tool run --isolated --from ruff==0.15.22 ruff format --check *.py tests scripts
"$HERMES_PYTHON" -m hermes_cli.main plugins doctor . --ci
"$HERMES_PYTHON" -m hermes_cli.main plugins validate . --install-deps
```

Use a fresh `HERMES_HOME`, never a real user's profile, for automated validation.
The explicit import prevents an accidentally missing Hermes dependency from
turning the full host suite into a misleading green skip. The plugin worker
stays idle outside the gateway; these commands do not start live reminders.

Live tests are separate: use disposable chats, explicitly authorize model usage
and synthetic delivery, and record acceptance, generation, and visible delivery
separately. Never commit real transcripts, auth files, profiles, databases, or
screenshots containing unrelated private content.

When adding a public source file, add it to `release-files.txt`. The exporter
includes only those exact paths. Review file content as well as the inventory;
an allowlist prevents accidental extra files, not secrets inserted in an allowed
file. See [release steps](docs/RELEASING.md).
