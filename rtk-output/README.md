# rtk-output

A self-contained Hermes plugin using the official, independently updatable `rtk`
executable. No core patches, command rewriting or sibling-plugin imports.

## Rules (0.1.1)

- Declare `requires_plugins: [shell-guards]` for host-managed load ordering. Hermes
  treats this as advisory: RTK still works when that plugin is absent, with a host
  warning. There are no sibling imports. When both are enabled, shell-guards runs
  first and its skill-size or deadline flag wins over compression. The host keeps
  only the first returned string; transformers do not chain. New third-party
  transformers on this seam need their own composition/precedence check.
- Register `transform_terminal_output`, after the foreground command finishes and
  before Hermes truncates and redacts its model-facing result. Never re-execute a
  command or change its argv, cwd, environment, approval or exit status.
- Consider successful outputs between 2,048 characters and 2 MiB. Keep failures,
  error/traceback indicators, already truncated output and short results unchanged.
- Use `rtk pipe` content autodetection. Recognize complete pytest summary lines to
  support quiet pytest, absolute venv interpreters, `env -u` and opaque scripts.
  Pytest status counts must survive in the shortened text. Other RTK auto-detected
  formats depend on the installed RTK version; this is not support for every RTK
  command-specific filter. There is no shell parser or command-pattern dependency.
- Redact secrets using Hermes' forced terminal redactor before passing text to RTK
  or saving it. A summary is accepted only when the filter exits successfully with
  no stderr, nonempty output, no error indicators and a net byte reduction including
  its recovery pointer. Missing RTK, timeouts (2 s), bad output or I/O errors pass
  the original result through. RTK never becomes a requirement for running commands.
- Save the full **redacted** filter input as a private, exclusively created file
  under the active home's `plugin-data/rtk-output/raw/`. Read the pointer with
  `read_file` or `search_files`; those tools are not filtered by this plugin. A
  sidecar JSON records the filter, before/after byte counts and original exit code.
  Files expire after seven days on a subsequent eligible call. At a 64 MiB retained
  directory size the plugin stops compressing until expired files can be removed.
  A single accepted output can exceed that soft quota by at most one input plus its
  small receipt; concurrent calls can temporarily exceed it too.

## Install and update

Install RTK through its official package manager (`brew install rtk` on macOS).
Install the whole monorepo, then enable its sub-plugin so updates retain Git
provenance:

```sh
hermes plugins install tmaarcxs/hermes-agent-guards --no-enable
hermes plugins enable tmaarcxs/hermes-agent-guards/rtk-output
```

If the monorepo is already installed, update it instead of reinstalling it.
`brew upgrade rtk` updates RTK separately. After either update, run this plugin's
E2E tests against the installed RTK. Disable with `hermes plugins disable rtk-output`;
this removes filtering, not the retained recovery files. Require a live hook probe
in the running gateway after activation; discovery alone is not activation.

## Verification and limits

```sh
python -m pytest -q rtk-output/tests/test_rtk_output_e2e.py
```

The tests load the real plugin in isolated Hermes homes and execute the real
terminal tool and official RTK. Only failure-injection scenarios substitute the
external RTK executable. Set `HERMES_RTK_E2E_CORE` when Hermes is not at
`~/.hermes/hermes-agent`. The selected interpreter needs pytest and the Hermes
runtime dependencies. No real account, model, chat or repository is contacted.

Only completed **foreground terminal** results reach this hook. Background jobs,
`process_manage`, other tools and already upstream-truncated output are not covered.
Failures deliberately remain uncompressed so diagnostics are not hidden. Native
Hermes output limits still apply. The plugin cannot recover bytes Hermes discarded
before the hook, and secret redaction is heuristic, not a guarantee of detecting
all sensitive text. RTK's own `gain` database does not account for `pipe` calls;
use the plugin sidecars for byte measurements, not token or billing claims.
