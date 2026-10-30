# SDK development

Read CLAUDE.md for repository build and code rules. Public plugin-authoring instructions
are in [docs/agent-guide.md](docs/agent-guide.md); the browser runtime contract is in
[docs/browser-runtime.md](docs/browser-runtime.md). Keep the runtime, packaged types,
helper tests and both author guides consistent. Preserve import-light imports and
worker job recovery. Browser teardown must never cancel remote jobs.
