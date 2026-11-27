# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
# The script's lines are kept whole, and its embedded Python is filled in with %.
# ruff: noqa: E501, UP031
"""The script a deployment's machine runs at first boot, rendered for any cloud.

:func:`render_deployment_script` turns a :class:`~tlc_plugin_sdk.infrastructure.DeploymentRequest` into the
bash an Ubuntu machine runs once as root: it installs ``uv``, a virtualenv and the two service packages,
writes the services' environment and systemd units, redeems the single-use boot token at the Config
Service for the two service keys, starts the Object Service and the Compute Service, and puts Caddy in
front of them for TLS. Progress goes to the machine's console as ``3LC_BOOT <stage>`` lines, which a
provider's ``deployment_state`` reads back.

What differs per cloud is a parameter: the environment that names the machine's identity, the object
store schemes the Object Service may reach, the command that prints the machine's public address, the
command that downloads a storage URL with the machine's identity, snippets to run before the install and
after the services start, and whether TLS ends on the machine or at a load balancer in front of it.

The script carries the services' environment and the boot token: it is the same trust class as the
machine itself, and belongs only in the channel a cloud gives a machine's first boot (user data, custom data).

Standard library only, and no import of the rest of the SDK at run time.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tlc_plugin_sdk.infrastructure.types import DeploymentRequest

__all__ = [
    "BOOT_LOG_FILE",
    "BOOT_STATUS_FILE",
    "COMPUTE_SERVICE_PORT",
    "CORE_PRERELEASES",
    "CORE_SPEC_ENV",
    "INDEX_STRATEGY",
    "IP_PLACEHOLDER",
    "JOURNAL_WATCH_PATH",
    "JOURNAL_WATCH_UNIT",
    "OBJECT_SERVICE_PORT",
    "REDACT_FUNCTION",
    "SERVICE_UNITS",
    "STAGING_CATALOG_ENV",
    "TLS_HOSTNAMES_FROM_IP",
    "UV_INSTALL",
    "UV_VERSION",
    "VERIFY_WHEEL_FUNCTION",
    "WHEEL_DIGEST_MARK",
    "InvalidBootInput",
    "caddy_block",
    "core_spec",
    "index_install_flags",
    "object_service_allowed_urls",
    "pin_wheel",
    "pinned_wheel",
    "render_deployment_script",
    "tls_hostnames_for_ip",
]


class InvalidBootInput(ValueError):
    """A value that cannot go into the boot script; the message says what to change."""


#: What joins a wheel's storage URL to the SHA-256 of the file the host built: ``<url>#sha256=<hex>``.
#: A wheel address always carries one. The wheel is installed as root on the machine, and whoever
#: can write the storage it is downloaded from could otherwise replace it.
WHEEL_DIGEST_MARK = "#sha256="
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: ``verify_wheel <file> <sha256>``: ends the boot, with a ``3LC_BOOT`` line, when the downloaded file
#: is not the one the host built. Never a reason to install from the index instead: a file that
#: differs was replaced or corrupted, and the index would hide it. Needs the script's ``report``.
VERIFY_WHEEL_FUNCTION = """verify_wheel() {
  local got
  got=$(sha256sum "$1" | cut -d' ' -f1)
  if [ "$got" != "$2" ]; then
    report "failed: wheel ${1##*/} is not the file the host built (sha256 $got, expected $2)"
    exit 1
  fi
}"""


def pin_wheel(url: str, sha256: str) -> str:
    """``url`` with the digest of the file it must hold: ``<url>#sha256=<hex>``.

    Raises:
        InvalidBootInput: When ``sha256`` is not 64 hex characters.
    """
    digest = sha256.strip().lower()
    if not _SHA256_RE.match(digest):
        msg = "A wheel's digest is the 64 hex characters of its SHA-256"
        raise InvalidBootInput(msg)
    return f"{url}{WHEEL_DIGEST_MARK}{digest}"


def pinned_wheel(address: str) -> tuple[str, str]:
    """The URL and the SHA-256 a wheel address names.

    Raises:
        InvalidBootInput: When the address carries no ``#sha256=<hex>`` or a malformed one.
    """
    url, mark, digest = address.strip().rpartition(WHEEL_DIGEST_MARK)
    digest = digest.lower()
    if not mark or not url or not _SHA256_RE.match(digest):
        name = address.strip().split("#", 1)[0].rsplit("/", 1)[-1][:120]
        msg = (
            f"The wheel {name!r} has no digest: a wheel address ends in {WHEEL_DIGEST_MARK}<the SHA-256 of the "
            "file>, so the machine can tell the file it downloads is the one that was built"
        )
        raise InvalidBootInput(msg)
    return url, digest


_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$", re.IGNORECASE
)


def _env_key(key: str) -> str:
    """An environment variable name: uppercase letters, digits and underscores, not starting with a digit."""
    text = str(key)
    if not _ENV_KEY_RE.fullmatch(text):
        msg = f"'{text}' is not a valid environment variable name: use uppercase letters, digits and underscores"
        raise InvalidBootInput(msg)
    return text


def _env_value(key: str, value: str) -> str:
    """An environment value that fits on one line of the machine's environment file."""
    text = str(value)
    if any(ch in text for ch in ("\n", "\r", "\0")):
        msg = f"The value of {key} contains a line break or a NUL character, which cannot go into the instance's environment"
        raise InvalidBootInput(msg)
    return text


def _hostname(value: str) -> str:
    """A DNS hostname (letters, digits, hyphens and dots), as it will appear in the Caddy configuration."""
    text = str(value or "").strip()
    if not _HOSTNAME_RE.fullmatch(text):
        msg = f"'{text}' is not a valid hostname for the workspace's TLS certificate"
        raise InvalidBootInput(msg)
    return text


# The uv release the bootstrap installs. Pinned: the installer is fetched over the network and runs as root.
UV_VERSION = "0.8.22"
UV_INSTALL = f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh"


_NODE_INDEX_PREFIX = "TLC_NODE_PIP_INDEX_"
_NODE_INDEX_URL_RE = re.compile(rf"^{_NODE_INDEX_PREFIX}([A-Z0-9_]+)_URL$")
INDEX_STRATEGY = "unsafe-first-match"  # the host's own: every index is searched, not just the first that answers
#: Installed beside the pin when the host sent extra indexes: a staged ``3lc-compute`` may floor a core that has no
#: release yet (``3lc>=X.Y.Z.devN``), and uv admits a pre-release only for a package whose *direct* requirement names
#: one. This line admits them for ``3lc`` alone; ``UV_PRERELEASE=allow`` would admit every third-party pre-release too
#: (it resolved a pandas ``rc`` for a staged agent).
CORE_PRERELEASES = "3lc>=0.0.0.dev0"
#: The create request's ``env`` entry naming the core to install instead: the launching host's own build or line.
#: The newest core on a staging index may be another line's (a ``3.6`` dev wheel above the host's ``3.5`` beta), and
#: only the host's build is sure to reach the host's Config Service. A directive for the boot script, like
#: :data:`STAGING_CATALOG_ENV`: it never reaches the machine's environment.
CORE_SPEC_ENV = "TLC_NODE_CORE_SPEC"

#: The create request's ``env`` entry that asks a deployment to install plugins from the staging index (see
#: :func:`_staging_index`). A directive for the boot script: it never reaches the machine's environment.
STAGING_CATALOG_ENV = "TLC_NODE_STAGING_CATALOG"
_CORE_SPEC_RE = re.compile(r"3lc\s*(?:==|>=|~=)\s*[0-9A-Za-z.]+(?:\s*,\s*(?:<|<=|!=)\s*[0-9A-Za-z.]+)*")
_STAGING_INDEX_NAME = "STAGING"


@dataclasses.dataclass(frozen=True)
class _StagingIndex:
    """The staging index a workspace installs plugins from, and the read credentials for it."""

    index_url: str
    username: str = dataclasses.field(repr=False)
    password: str = dataclasses.field(repr=False)


def _staging_index(env: dict[str, str]) -> _StagingIndex | None:
    """The staging index the workspace's compute installs plugins from, when the host asked for it.

    A launcher on a pre-release (staged) line sends ``TLC_NODE_STAGING_CATALOG=1`` with the staging
    index's node read credentials (``TLC_NODE_PIP_INDEX_STAGING_URL`` / ``_USERNAME`` / ``_PASSWORD``,
    the ones that already carry its services' install). The catalogs the workspace reads (its Config
    Service's, and those the host sent) pin staged builds, which exist only on that index.

    Raises:
        InvalidBootInput: The host asked for it but sent no staging index with credentials.
    """
    if env.get(STAGING_CATALOG_ENV, "").strip() != "1":
        return None
    prefix = f"{_NODE_INDEX_PREFIX}{_STAGING_INDEX_NAME}_"
    url, username, password = (env.get(f"{prefix}{field}", "").strip() for field in ("URL", "USERNAME", "PASSWORD"))
    if not (url and username and password):
        msg = (
            f"{STAGING_CATALOG_ENV} asks the workspace to install plugins from the staging index, but the request carries no "
            "staging index with read credentials"
        )
        raise InvalidBootInput(msg)
    return _StagingIndex(index_url=url, username=username, password=password)


def core_spec(env: dict[str, str], *, default: str) -> str:
    """The ``3lc`` requirement to install beside ``3lc-compute``: the host's (:data:`CORE_SPEC_ENV`), else ``default``.

    Raises:
        InvalidBootInput: The host sent something that is not a requirement on ``3lc``.
    """
    spec = env.get(CORE_SPEC_ENV, "").strip()
    if not spec:
        return default
    if not _CORE_SPEC_RE.fullmatch(spec):
        msg = f"{CORE_SPEC_ENV} must be a version requirement on 3lc"
        raise InvalidBootInput(msg)
    return spec


def _join_urls(*groups: str) -> str:
    """Comma-joined URL lists, each entry once, in order."""
    seen: list[str] = []
    for group in groups:
        for url in (part.strip() for part in group.split(",")):
            if url and url not in seen:
                seen.append(url)
    return ",".join(seen)


def index_install_flags(env: dict[str, str], *, inline_credentials: bool = False) -> tuple[str, str]:
    """The uv index flags for the node's extra indexes, and the exports that authenticate them.

    Mirrors what the node agent does for its own installs: each URL in ``TLC_NODE_PIP_INDEX_URLS``
    (comma-separated) is an extra index searched before PyPI under :data:`INDEX_STRATEGY`. A URL
    the host sent credentials for (``TLC_NODE_PIP_INDEX_<NAME>_URL`` naming it, with ``_USERNAME``
    and ``_PASSWORD``) becomes uv's named ``--index <name>=<url>``, which uv authenticates from
    ``UV_INDEX_<NAME>_USERNAME`` / ``_PASSWORD``; the export lines set those from the
    ``TLC_NODE_PIP_INDEX_<NAME>_*`` variables the script already exported, so no credential value
    is written a second time and none ever rides a URL or a command line. A script that exports
    nothing into its own shell (the workspace's: its env goes to a systemd file) asks for
    ``inline_credentials`` and gets the values, shell-quoted, in the export lines instead.

    Returns:
        ``(flags, exports)``: the flags to splice into the ``uv pip install`` line (``""`` without
        indexes) and the ``export UV_INDEX_…`` lines (``""`` without credentials).
    """
    urls = [url.strip() for url in env.get("TLC_NODE_PIP_INDEX_URLS", "").split(",") if url.strip()]
    if not urls:
        return "", ""
    credentialed: dict[str, str] = {}  # url -> NAME
    for key, value in env.items():
        match = _NODE_INDEX_URL_RE.match(key)
        if not match:
            continue
        name = match.group(1)
        username = env.get(f"{_NODE_INDEX_PREFIX}{name}_USERNAME", "")
        password = env.get(f"{_NODE_INDEX_PREFIX}{name}_PASSWORD", "")
        if value.strip() and username and password:
            credentialed[value.strip()] = name
    flags = [f"--index-strategy {INDEX_STRATEGY}"]
    exports: list[str] = []
    for url in urls:
        name = credentialed.get(url)
        if name is None:
            flags.append(f"--extra-index-url {shlex.quote(url)}")
            continue
        flags.append(f"--index {shlex.quote(f'{name.lower()}={url}')}")
        for field in ("USERNAME", "PASSWORD"):
            source = f"{_NODE_INDEX_PREFIX}{name}_{field}"
            value = shlex.quote(env[source]) if inline_credentials else f'"${source}"'
            exports.append(f"export UV_INDEX_{name}_{field}={value}")
    return " ".join(flags), "\n".join(exports)


# ── the deployment's services ────────────────────────────────────────────────

OBJECT_SERVICE_PORT = 5015
COMPUTE_SERVICE_PORT = 5020
TLC_DIST_SPEC = "3lc"  # the Object Service dist installed next to 3lc-compute
BOOT_LOG_FILE = "/var/log/3lc-workspace-boot.log"  # everything the script prints
BOOT_STATUS_FILE = "/etc/3lc/boot-status"  # one line per stage; ``tls=failed …`` when Caddy did not install
_ENV_EOF = "__3LC_ENV__"
_UNIT_EOF = "__3LC_UNIT__"


def _env_file_lines(env: dict[str, str]) -> str:
    """systemd ``EnvironmentFile`` syntax: ``KEY="value"`` with backslash-escaped quotes.

    The file is written through a quoted heredoc that ends at a line equal to the delimiter, so a
    value with a line break could end the file early and run what follows as root. Such values,
    and keys that are not variable names, are refused (:class:`InvalidBootInput`).
    """
    lines = []
    for key, value in sorted(env.items()):
        _env_key(key)
        text = _env_value(key, value)
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines)


#: Per-service environment files the units read after the shared one: the keys the bootstrap wrote.
OBJECT_SERVICE_ENV_FILE = "/etc/3lc/env.object-service"
COMPUTE_SERVICE_ENV_FILE = "/etc/3lc/env.compute"
_BOOTSTRAP_ANSWER = "/etc/3lc/bootstrap.json"
_KEYS_EOF = "__3LC_KEYS__"
# Turns the redeem answer into the two env files; runs under the service venv's Python, as root, with
# a 077 umask. JSON quoting is systemd's double-quote syntax for the characters a key can hold.
_WRITE_KEYS_PY = """import json
with open("%(answer)s") as f:
    answer = json.load(f)
for field, path in (("object_service", "%(object)s"), ("compute_service", "%(compute)s")):
    with open(path, "w") as f:
        f.write("TLC_API_KEY=" + json.dumps(str(answer[field]["api_key"])) + "\\n")
""" % {
    "answer": _BOOTSTRAP_ANSWER,
    "object": OBJECT_SERVICE_ENV_FILE,
    "compute": COMPUTE_SERVICE_ENV_FILE,
}


def _redeem_url(config_service_url: str, record_id: str) -> str:
    """Where a deployment's machine redeems its boot token."""
    return f"{config_service_url.rstrip('/')}/v1/workspaces/{record_id}/bootstrap"


def _redeem_block(redeem_url: str, token: str) -> str:
    """The boot-script lines that turn the bootstrap token into the two services' keys.

    Transient failures (the Config Service unreachable, a 5xx) are retried a few times; a refusal
    (the token spent, expired or withdrawn) is not: it is reported on the console as a failed stage
    and the script stops, so the host sees the workspace die instead of two services without keys.
    """
    if not redeem_url:
        return ""
    body = json.dumps({"token": token})
    return f"""stage="redeeming the workspace's keys"
report "$stage"
umask 077
if ! curl --fail-with-body -sS --retry 5 --retry-delay 5 -X POST {shlex.quote(redeem_url)} \\
  -H 'Content-Type: application/json' -d {shlex.quote(body)} -o {_BOOTSTRAP_ANSWER}; then
  echo "bootstrap=failed: the Config Service did not redeem the bootstrap token" >> {BOOT_STATUS_FILE}
  false
fi
/opt/3lc/bin/python - <<'{_KEYS_EOF}'
{_WRITE_KEYS_PY}{_KEYS_EOF}
rm -f {_BOOTSTRAP_ANSWER}
umask 022
echo "bootstrap=redeemed" >> {BOOT_STATUS_FILE}
"""


def _unit(description: str, exec_start: str, *, after: str = "network-online.target", env_file: str = "") -> str:
    """A systemd unit; ``env_file`` is a second, optional environment file read after the shared one."""
    extra = f"EnvironmentFile=-{env_file}\n" if env_file else ""
    return f"""[Unit]
Description={description}
After={after}
Wants=network-online.target

[Service]
EnvironmentFile=/etc/3lc/env
{extra}Environment=HOME=/root
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=/srv/3lc
ExecStart={exec_start}
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


# ── service supervision ──────────────────────────────────────────────────────
#
# A service that crash-loops after boot leaves no trace a launcher can read without a login on the
# machine, so the console is the channel out. A small watcher (its own systemd unit, started once the
# services are) writes the tail of a failing unit's journal there, between ``3LC_JOURNAL_BEGIN`` /
# ``3LC_JOURNAL_END`` markers, for a provider's ``deployment_state`` diagnostics to read back.

SERVICE_UNITS = ("3lc-object-service", "3lc-compute")
JOURNAL_WATCH_PATH = "/usr/local/bin/3lc-journal-watch"
JOURNAL_WATCH_UNIT = "3lc-journal-watch"
_WATCH_EOF = "__3LC_WATCH__"


def _any_case(word: str) -> str:
    return "".join(f"[{c.upper()}{c.lower()}]" for c in word)


# A name that says its value is a secret, in any case. Spelled out per letter: the script runs
# wherever ``sed -E`` does, with no case-insensitive flag.
_SECRET_NAME = "(" + "|".join(_any_case(w) for w in ("key", "token", "secret", "password", "credential")) + ")"
# A single quote inside the single-quoted sed programs below.
_Q = "'\"'\"'"

#: Shell function ``redact``: a filter that masks secrets in log lines before they reach the console.
#: Every boot script pipes what it prints of a log through it. In order: a service key of any length
#: (``tlc_svc_…``), a 3LC token of any other kind (``tlc_<kind>_…``, twenty characters or more: a
#: bootstrap token, a personal key), a ``Bearer`` value,
#: a quoted ``"…key…": "value"`` pair (JSON, or a Python repr), a ``NAME_WITH_KEY=value`` assignment,
#: and a run of 24 or more base64, hex or URL-safe characters within two words after a name that says secret. The
#: names are ``key``, ``token``, ``secret``, ``password`` and ``credential``, anywhere in a name.
REDACT_FUNCTION = (
    r"""redact() {
  sed -E -e 's/tlc_svc_[A-Za-z0-9_-]+/tlc_svc_REDACTED/g' \
    -e 's/(tlc_[A-Za-z0-9]+_)[A-Za-z0-9_-]{12,}/\1REDACTED/g' \
    -e 's/(@BEARER@ +)[A-Za-z0-9._~+\/=-]+/\1REDACTED/g' \
    -e 's/(["@Q@][A-Za-z0-9_.-]*@NAME@[A-Za-z0-9_.-]*["@Q@] *: *["@Q@])[^"@Q@]*/\1REDACTED/g' \
    -e 's/([A-Za-z0-9_]*@NAME@[A-Za-z0-9_]*=)[^[:space:]]*/\1REDACTED/g' \
    -e 's/(@NAME@[^[:space:]]*( +[A-Za-z]+){0,2}[ :=]+["@Q@]?)[A-Za-z0-9+\/=_-]{24,}/\1REDACTED/g'
}
"""
    .replace("@BEARER@", _any_case("bearer"))
    .replace("@NAME@", _SECRET_NAME)
    .replace("@Q@", _Q)
)
# Plain text (not an f-string): the shell's ``$`` and braces stay as written. Bounds, so a restart
# loop cannot flood the console (which keeps only its last 64 KB anyway): at most MAX_DUMPS
# dumps per unit of TAIL_LINES lines each, a unit's next dump only after RESTARTS_BETWEEN further
# restarts, and the watcher stops after WATCH_SECONDS or once every unit has used its dumps.
_JOURNAL_WATCH_SH = r"""#!/bin/bash
UNITS="@UNITS@"
TAIL_LINES=40
MAX_DUMPS=3
FIRST_DUMP_AT=3
RESTARTS_BETWEEN=20
READY_SECONDS=300
WATCH_SECONDS=3600
# Journal lines only (never an environment file), secrets masked before they reach the console.
@REDACT@declare -A dumps next failed_seen slow_seen
for u in $UNITS; do dumps[$u]=0; next[$u]=$FIRST_DUMP_AT; done
dump() {
  local u=$1 reason=$2
  dumps[$u]=$((dumps[$u] + 1))
  {
    printf '3LC_JOURNAL_BEGIN %s %s\n' "$u" "$reason"
    journalctl -u "$u" -n "$TAIL_LINES" --no-pager -o short-iso 2>&1 | redact | cut -c1-200 | sed "s/^/3LC_JOURNAL $u /"
    printf '3LC_JOURNAL_END %s\n' "$u"
  } > /dev/console 2>/dev/null || true
}
start=$SECONDS
while [ $((SECONDS - start)) -lt $WATCH_SECONDS ]; do
  active=0
  for u in $UNITS; do
    [ "${dumps[$u]}" -lt "$MAX_DUMPS" ] || continue
    active=1
    state=$(systemctl show -p ActiveState --value "$u" 2>/dev/null)
    result=$(systemctl show -p Result --value "$u" 2>/dev/null)
    restarts=$(systemctl show -p NRestarts --value "$u" 2>/dev/null)
    [[ $restarts =~ ^[0-9]+$ ]] || restarts=0
    summary="restarts=$restarts,state=${state:-unknown},result=${result:-unknown}"
    if [ "$state" = failed ] && [ -z "${failed_seen[$u]}" ]; then
      failed_seen[$u]=1
      dump "$u" "failed;$summary"
    elif [ "$restarts" -ge "${next[$u]}" ]; then
      next[$u]=$((restarts + RESTARTS_BETWEEN))
      dump "$u" "restarting;$summary"
    elif [ $((SECONDS - start)) -ge $READY_SECONDS ] && [ "$state" != active ] && [ -z "${slow_seen[$u]}" ]; then
      slow_seen[$u]=1
      dump "$u" "not-ready;$summary"
    fi
  done
  [ "$active" = 1 ] || break
  sleep 10
done
""".replace("@UNITS@", " ".join(SERVICE_UNITS)).replace("@REDACT@", REDACT_FUNCTION)

_JOURNAL_WATCH_UNIT = """[Unit]
Description=3LC service journal watcher (boot console diagnostics)
After=@AFTER@

[Service]
Type=simple
ExecStart=@PATH@
""".replace("@AFTER@", " ".join(f"{u}.service" for u in SERVICE_UNITS)).replace("@PATH@", JOURNAL_WATCH_PATH)


def _supervision_block() -> str:
    """The boot-script lines that install and start the journal watcher; it never fails the boot."""
    return f"""cat > {JOURNAL_WATCH_PATH} <<'{_WATCH_EOF}'
{_JOURNAL_WATCH_SH}{_WATCH_EOF}
chmod 755 {JOURNAL_WATCH_PATH}
cat > /etc/systemd/system/{JOURNAL_WATCH_UNIT}.service <<'{_UNIT_EOF}'
{_JOURNAL_WATCH_UNIT}{_UNIT_EOF}
systemctl daemon-reload
systemctl start --no-block {JOURNAL_WATCH_UNIT} || true
"""


#: The demo host's local, empty project root; a full workspace names its root or takes it centrally.
DEMO_PROJECT_ROOT = "/srv/3lc/projects"

#: The environment names that carry a project root to the core (canonical and the config alias).
ROOT_ENV_KEYS = ("TLC_PROJECT_ROOT_URL", "TLC_CONFIG_PROJECT_ROOT_URL")

#: The Object Service's storage bound (``service.allowed-urls``): only that service honours it.
ALLOWED_URLS_ENV = "TLC_SERVICE_ALLOWED_URLS"

#: The core's default project root on a workspace (services run as root, ``HOME=/root``): where a
#: workspace without a named or central root keeps its projects.
WORKSPACE_DEFAULT_ROOT = "/root/.local/share/3LC/projects"


def object_service_allowed_urls(project_root_url: str = "", *, schemes: Sequence[str] = ()) -> str:
    """The storage a full deployment's Object Service may reach, as ``TLC_SERVICE_ALLOWED_URLS``.

    Every URL of the provider's object store ``schemes`` (``"s3://"``, ``"abfs://"``): the machine's
    identity decides which containers the service reaches, so roots and scan folders chosen later keep
    working. On the local disk, only the project roots a deployment uses (the demo root folder, the core's
    default, and a named local root). Everything else is refused: other local files (the services'
    environment files, ``/proc``) and every ``http(s)`` URL.
    """
    local = [DEMO_PROJECT_ROOT, WORKSPACE_DEFAULT_ROOT]
    root = project_root_url.strip().rstrip("/")
    if root.startswith("/") and not any(root == u or root.startswith(u + "/") for u in local):
        local.append(root)
    return ",".join([*schemes, *local])


def render_deployment_script(
    request: DeploymentRequest,
    *,
    default_compute_spec: str,
    extra_specs: Sequence[str] = (),
    identity_env: Mapping[str, str] | None = None,
    storage_schemes: Sequence[str] = (),
    confine_object_service: bool = True,
    public_address_command: str = "",
    download_command: str = "",
    wheel_urls: Sequence[str] = (),
    before_install: str = "",
    after_services: str = "",
    tls: Literal["machine", "load_balancer", "none"] = "machine",
    backend_tls: bool = False,
    wait_for_dns: bool = True,
    wait_for_health: bool = False,
) -> str:
    """The first-boot script of a deployment's machine: install ``3lc`` and ``3lc-compute``, run both as services.

    From ``request``: ``env`` is the environment both services inherit, and carries the install
    directives of the launching host, which never reach the services (``TLC_NODE_PIP_INDEX_URLS`` and the
    ``TLC_NODE_PIP_INDEX_<NAME>_URL`` / ``_USERNAME`` / ``_PASSWORD`` credentials of an index,
    :data:`CORE_SPEC_ENV`, :data:`STAGING_CATALOG_ENV`). ``compute_spec`` is the requirement for
    ``3lc-compute``. ``plugins`` are the install sources the Compute Service installs at first start.
    ``mode`` is the Compute Service's mode; ``demo`` also gets a local, empty project root, the public
    tutorial projects and no default plugins. ``storage.root`` is written into both services' environment;
    an empty one writes none, so the services take the root the Config Service holds, else the core's
    local default. ``storage.scan`` are folders to index. With ``storage.central`` neither is written:
    the Config Service holds both as the deployment's settings, and they only bound the Object Service. With ``bootstrap.record_id`` and
    ``bootstrap.token`` the machine redeems the token at the Config Service before the services start and
    each service reads its own key from its own environment file; with both empty it redeems nothing.
    ``hostnames`` are the names Caddy serves; ``None`` names the deployment by its public address under
    sslip.io (:data:`TLS_HOSTNAMES_FROM_IP`).

    A root, scan folders, allowed URLs, an API key or node-agent settings the launching host carries in
    ``env`` are dropped: they are the launcher's own.

    Args:
        request: The deployment asked for.
        default_compute_spec: The requirement for ``3lc-compute`` when the request names none.
        extra_specs: Further requirements installed beside the two services.
        identity_env: Environment that tells the services which identity and location the machine has
            (``AWS_DEFAULT_REGION``, ``AZURE_CLIENT_ID``); laid over ``request.env``.
        storage_schemes: The provider's object store URL schemes, for :func:`object_service_allowed_urls`.
        confine_object_service: Bound a full deployment's Object Service to those URLs.
        public_address_command: Shell that prints the machine's public IPv4 address. Needed when the names
            derive from the address, and for ``wait_for_dns``.
        download_command: Shell that downloads the storage URL ``$1`` into the directory ``$2`` with the
            machine's identity. Needed with ``wheel_urls``.
        wheel_urls: Storage URLs of wheels installed in place of the ``3lc-compute`` requirement, each
            ``<url>#sha256=<hex>`` (:func:`pinned_wheel`). When a download fails the requirement is
            installed from the index instead; a downloaded file with another digest fails the boot.
        before_install: Shell lines run before the first package install (a mirror choice, say).
        after_services: Shell lines run once the services are started (enabling a cloud's agent, say).
        tls: ``machine``: Caddy obtains the certificates on the machine and serves 443. ``load_balancer``:
            a load balancer in front holds the certificate and Caddy routes by host name behind it.
            ``none``: no Caddy; the services answer on their own ports.
        backend_tls: With ``load_balancer``, serve the load balancer over HTTPS with a self-signed
            certificate made on the machine, on 443, instead of HTTP on 80.
        wait_for_dns: With ``machine`` and names in a zone, wait up to five minutes for each name to
            resolve to the machine's public address before Caddy asks for certificates, so the first
            attempt is not spent on a name that does not exist yet. Caddy retries by itself afterwards.
        wait_for_health: Wait up to five minutes per service for both to answer on the machine before the
            script reports the boot finished; one that never answers fails the boot, with each service's
            journal tail on the console.

    Returns:
        The script, starting with ``#!/bin/bash``.

    Raises:
        InvalidBootInput: An environment key or value, a hostname or an install directive cannot go into
            the script, or a parameter a chosen variant needs is missing.
    """
    env = {**request.env, **(identity_env or {})}
    mode = request.mode or "full"
    project_root_url = request.storage.root
    project_scan_urls = list(request.storage.scan)
    redeem_url = ""
    if request.bootstrap.record_id and request.bootstrap.token:
        redeem_url = _redeem_url(request.bootstrap.config_service_url, request.bootstrap.record_id)
    tls_hostnames: dict[str, str] | None = None
    if tls != "none":
        names = request.hostnames
        if names is not None:
            tls_hostnames = {"object": names.object_service, "compute": names.compute_service}
        elif tls == "machine":
            tls_hostnames = dict(TLS_HOSTNAMES_FROM_IP)
        else:
            msg = "A deployment behind a load balancer needs the hostnames the load balancer routes"
            raise InvalidBootInput(msg)
    service_env = {
        **env,
        "TLC_COMPUTE_HOST": "0.0.0.0",
        "TLC_COMPUTE_PORT": str(COMPUTE_SERVICE_PORT),
        "TLC_COMPUTE_MODE": mode,
        "TLC_COMPUTE_PLUGIN_INSTALL_POLICY": "any",
        "TLC_COMPUTE_UV": "/root/.local/bin/uv",
        "TLC_COMPUTE_BOOTSTRAP_PLUGINS": ";".join(request.plugins),
    }
    # The root is named here or nowhere: a root the launching host happens to carry in its own
    # environment is not this deployment's.
    for key in ROOT_ENV_KEYS:
        service_env.pop(key, None)
    # Explicit scan entries merge with central scan-urls; never copy the launcher's own scan roots.
    service_env.pop("TLC_SCAN_URLS", None)
    service_env.pop("TLC_CONFIG_PROJECT_SCAN_URLS", None)
    # Settings the Config Service holds are read from it: an environment value would outrank them
    # and pin the deployment to what it was launched with.
    central = request.storage.central and mode != "demo"
    if project_scan_urls and not central:
        service_env["TLC_SCAN_URLS"] = ",".join(project_scan_urls)
    if project_root_url and not central:
        service_env["TLC_PROJECT_ROOT_URL"] = project_root_url
    elif mode == "demo":
        service_env["TLC_PROJECT_ROOT_URL"] = DEMO_PROJECT_ROOT
    if mode == "demo":
        service_env["TLC_COMPUTE_DEFAULT_PLUGINS"] = ""  # infra plugins only; nothing else installs
    # Like the root, the bound is this deployment's own: never one the launching host carries.
    service_env.pop(ALLOWED_URLS_ENV, None)
    if confine_object_service and mode != "demo":
        service_env[ALLOWED_URLS_ENV] = object_service_allowed_urls(project_root_url, schemes=storage_schemes)
    staging = _staging_index(env)
    service_env.pop(STAGING_CATALOG_ENV, None)
    service_env.pop(CORE_SPEC_ENV, None)
    if staging is not None:
        # The deployment lists the catalogs it is configured with and installs what they pin from this index.
        service_env["TLC_COMPUTE_PLUGIN_INDEX_URLS"] = _join_urls(
            service_env.get("TLC_COMPUTE_PLUGIN_INDEX_URLS", ""), f"staging={staging.index_url}"
        )
        service_env[f"UV_INDEX_{_STAGING_INDEX_NAME}_USERNAME"] = staging.username
        service_env[f"UV_INDEX_{_STAGING_INDEX_NAME}_PASSWORD"] = staging.password
    if redeem_url:
        service_env["TLC_CONFIG_SERVICE_URL"] = request.bootstrap.config_service_url
        service_env.pop("TLC_API_KEY", None)  # each service's own key comes from the bootstrap, not from here
    # Node-agent-only knobs make no sense on a deployment; keep the file honest.
    for key in ("TLC_NODE_TOKEN", "TLC_NODE_IDLE_TTL_S", "TLC_NODE_TERMINATE_CMD", "TLC_NODE_STAGE"):
        service_env.pop(key, None)
    # A deployment of one's own indexes its own projects; the public tutorial projects belong to the
    # 3LC-hosted demo only (they are what a visitor browses there).
    examples = "" if mode == "demo" else " --no-public-examples"
    object_unit = _unit(
        "3LC Object Service",
        f"/opt/3lc/bin/3lc service --host 0.0.0.0 --port {OBJECT_SERVICE_PORT}{examples}",
        env_file=OBJECT_SERVICE_ENV_FILE,
    )
    compute_unit = _unit(
        "3LC Compute Service",
        "/opt/3lc/bin/3lc-compute",
        after="3lc-object-service.service",
        env_file=COMPUTE_SERVICE_ENV_FILE,
    )
    compute = request.compute_spec.strip() or default_compute_spec
    index_flags, index_exports = index_install_flags(env, inline_credentials=True)
    core = core_spec(env, default=CORE_PRERELEASES if index_flags else TLC_DIST_SPEC)
    specs = " ".join(shlex.quote(spec) for spec in (core, compute, *extra_specs) if spec)
    authenticate = index_exports + "\n" if index_exports else ""
    install_flags = " " + index_flags if index_flags else ""
    install = f"{authenticate}uv pip install --python /opt/3lc/bin/python{install_flags} {specs}\n"
    if wheel_urls:
        if not download_command.strip():
            msg = "Wheel URLs need the command that downloads them with the machine's identity"
            raise InvalidBootInput(msg)
        others = " ".join(shlex.quote(spec) for spec in (core, *extra_specs) if spec)
        install = (
            _wheels_block(wheel_urls, download_command, fallback=compute)
            + f'{authenticate}uv pip install --python /opt/3lc/bin/python{install_flags} {others} "${{INSTALL[@]}}"\n'
        )
    caddy = caddy_block(
        tls_hostnames,
        behind_load_balancer=tls == "load_balancer",
        backend_tls=backend_tls,
        public_address_command=public_address_command,
        wait_for_dns=wait_for_dns,
    )
    return f"""#!/bin/bash
set -e
export HOME=/root
exec > {BOOT_LOG_FILE} 2>&1
stage=initializing
report() {{ printf '3LC_BOOT %s\\n' "$1" > /dev/console; }}
trap 'report "failed: $stage (exit $?)"' ERR
export DEBIAN_FRONTEND=noninteractive
# apt with bounded waits and one retry.
APT="apt-get -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10 -o Acquire::Retries=1 -o Acquire::Languages=none"
{_snippet(before_install)}if ! timeout 120 $APT update -y >/dev/null 2>&1; then
  echo "apt: update did not finish in 120s; trying once more"
  timeout 120 $APT update -y >/dev/null 2>&1 || true
fi
# Compilers and Python headers: the odd dependency ships no Linux wheel and builds from
# source (seen live: annoy, via 3lc[pacmap] in the YOLO plugin — "No such file or
# directory: 'x86_64-linux-gnu-g++'" when the user installed it on a bare workspace).
# libgl1 + libglib2.0-0: OpenCV's wheel (ultralytics, rf-detr, sam3 pull it in) dlopens libGL
# even headless; without them the plugin imports fail and the model dropdown is empty (found live).
timeout 900 $APT install -y curl ca-certificates build-essential python3-dev libgl1 libglib2.0-0 >/dev/null 2>&1 || true
stage="installing the services"
report "$stage"
command -v uv >/dev/null 2>&1 || ({UV_INSTALL})
export PATH="$HOME/.local/bin:$PATH"
uv venv /opt/3lc --python 3.12
{install}mkdir -p /etc/3lc /srv/3lc/projects
install -m 600 /dev/null /etc/3lc/env
cat > /etc/3lc/env <<'{_ENV_EOF}'
{_env_file_lines(service_env)}
{_ENV_EOF}
chmod 600 /etc/3lc/env
cat > /etc/systemd/system/3lc-object-service.service <<'{_UNIT_EOF}'
{object_unit}{_UNIT_EOF}
cat > /etc/systemd/system/3lc-compute.service <<'{_UNIT_EOF}'
{compute_unit}{_UNIT_EOF}
{_redeem_block(redeem_url, request.bootstrap.token)}stage="starting the services"
report "$stage"
systemctl daemon-reload
systemctl enable --now 3lc-object-service 3lc-compute
echo "services=started" >> {BOOT_STATUS_FILE}
{_supervision_block()}{_snippet(after_services)}{caddy}{_health_block(wait_for_health)}echo "3lc workspace ready"
report "boot finished"
"""


def _snippet(text: str) -> str:
    """A provider's shell lines, ending in exactly one newline; ``""`` stays empty."""
    return text.rstrip("\n") + "\n" if text.strip() else ""


def _wheels_block(urls: Sequence[str], download_command: str, *, fallback: str) -> str:
    """Shell that downloads ``urls`` into a temp directory and sets ``INSTALL`` to the wheels, else to ``fallback``.

    Each URL gets three tries; one that still fails sends the install back to the index requirement, with
    a ``3LC_BOOT`` line saying so. A file that arrives with another digest than its address names ends
    the boot (:data:`VERIFY_WHEEL_FUNCTION`).

    Raises:
        InvalidBootInput: When an address carries no digest.
    """
    pins = [pinned_wheel(address) for address in urls]
    quoted = " ".join(shlex.quote(f"{digest} {url}") for url, digest in pins)
    return f"""fetch_wheel() {{ {download_command.strip()}; }}
{VERIFY_WHEEL_FUNCTION}
stage="downloading service wheels"
report "$stage"
wheel_dir=$(mktemp -d)
wheels_ok=1
for pin in {quoted}; do
  url=${{pin#* }}
  fetched=0
  for attempt in 1 2 3; do
    if fetch_wheel "$url" "$wheel_dir/"; then fetched=1; break; fi
    sleep $((attempt * 2))
  done
  if [ "$fetched" != 1 ]; then wheels_ok=0; break; fi
  verify_wheel "$wheel_dir/${{url##*/}}" "${{pin%% *}}"
done
if [ "$wheels_ok" = 1 ] && compgen -G "$wheel_dir/*.whl" >/dev/null; then
  INSTALL=("$wheel_dir"/*.whl)
else
  report "service wheels unavailable: installing from the index"
  INSTALL=({shlex.quote(fallback)})
fi
stage="installing the services"
"""


def _health_block(enabled: bool) -> str:
    """Shell that waits up to five minutes per service for both to answer HTTP on the machine.

    A service that never answers fails the boot: the tail of each service's journal goes to the console
    between ``3LC_JOURNAL_BEGIN <unit> unanswered`` / ``3LC_JOURNAL_END`` markers, then
    ``3LC_BOOT failed: waiting for the services to answer (exit 1)``, and the script stops before
    ``boot finished``.
    """
    if not enabled:
        return ""
    units = " ".join(SERVICE_UNITS)
    return f"""stage="waiting for the services to answer"
report "$stage"
unanswered=""
for port in {OBJECT_SERVICE_PORT} {COMPUTE_SERVICE_PORT}; do
  answered=0
  for attempt in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 5 "http://127.0.0.1:$port/"; then answered=1; break; fi
    sleep 5
  done
  [ "$answered" = 1 ] || unanswered="$unanswered $port"
done
if [ -n "$unanswered" ]; then
  echo "health=failed: nothing answered on port(s)$unanswered in 300s" >> {BOOT_STATUS_FILE}
{REDACT_FUNCTION}  for u in {units}; do
    {{
      printf '3LC_JOURNAL_BEGIN %s unanswered\\n' "$u"
      journalctl -u "$u" -n 40 --no-pager -o short-iso 2>&1 | redact | cut -c1-200 | sed "s/^/3LC_JOURNAL $u /"
      printf '3LC_JOURNAL_END %s\\n' "$u"
    }} > /dev/console 2>/dev/null || true
  done
  report "failed: $stage (exit 1)"
  exit 1
fi
"""


def _dns_wait_block(names: Sequence[str], public_address_command: str) -> str:
    """Shell that waits for ``names`` to resolve to the machine's public address; it never fails the boot."""
    quoted = " ".join(shlex.quote(name) for name in names)
    return (
        'stage="waiting for DNS to name this machine"\n'
        'report "$stage"\n'
        f'PUBIP=$({public_address_command.strip()}) || PUBIP=""\n'
        'if [ -n "$PUBIP" ]; then\n'
        f"for name in {quoted}; do\n"
        "  resolved=0\n"
        "  for attempt in $(seq 1 60); do\n"
        '    if getent ahostsv4 "$name" 2>/dev/null | cut -d\' \' -f1 | grep -qxF "$PUBIP"; then resolved=1; break; fi\n'
        "    sleep 5\n"
        "  done\n"
        f'  [ "$resolved" = 1 ] || echo "dns=slow: $name did not resolve to $PUBIP in 300s" >> {BOOT_STATUS_FILE}\n'
        "done\n"
        "fi\n"
        'stage="obtaining certificates"\n'
    )


IP_PLACEHOLDER = "{ip}"
# The default TLS names of a workspace: sslip.io resolves ``os.1-2-3-4.sslip.io`` to 1.2.3.4, so every
# workspace gets Let's Encrypt certificates under names derived from its own public IP. Without TLS an
# https:// Hub (the hosted one) cannot call the workspace at all — the browser blocks mixed content.
# A launcher with a DNS zone allocates names in it instead; those names reach :func:`caddy_block` whole.
TLS_HOSTNAMES_FROM_IP: dict[str, str] = {"object": "os.{ip}.sslip.io", "compute": "compute.{ip}.sslip.io"}


def tls_hostnames_for_ip(public_ip: str, names: dict[str, str] | None = None) -> dict[str, str]:
    """Fill the ``{ip}`` placeholder with the dashed public IP: ``os.{ip}.sslip.io`` → ``os.1-2-3-4.sslip.io``."""
    dashed = public_ip.replace(".", "-")
    return {k: v.replace(IP_PLACEHOLDER, dashed) for k, v in (names or TLS_HOSTNAMES_FROM_IP).items()}


def caddy_block(
    tls_hostnames: dict[str, str] | None,
    *,
    behind_load_balancer: bool = False,
    backend_tls: bool = False,
    public_address_command: str = "",
    wait_for_dns: bool = False,
) -> str:
    """Install Caddy and terminate TLS for the two services under their own hostnames.

    ``tls_hostnames`` is ``{"object": "os.<ip>.sslip.io", "compute": "compute.<ip>.sslip.io"}``:
    two names on 443 (SNI routes them), certificates from Let's Encrypt automatically, HTTP→HTTPS
    redirects, websockets proxied. This is what lets an ``https://`` Hub call the services at all.
    Ports 80 and 443 must be open to the machine.

    When Caddy does not install (its repository unreachable, say) the services still start and
    ``/etc/3lc/boot-status`` records ``tls=failed`` for the operator to read; the https
    URLs the provider returned will not answer until Caddy is installed by hand.
    """
    if not tls_hostnames:
        return ""
    obj, comp = tls_hostnames.get("object", ""), tls_hostnames.get("compute", "")
    if not obj or not comp:
        return ""
    # The names go into a Caddyfile written by the boot script: only hostnames may pass.
    for name in (obj, comp):
        _hostname(name.replace(IP_PLACEHOLDER, "1-2-3-4"))
    # Names may carry the ``{ip}`` placeholder (see :data:`TLS_HOSTNAMES_FROM_IP`): a deployment named
    # under sslip.io learns its public address, and therefore its names, only once the machine is up. The
    # script asks for the address and fills the names in itself. A deployment with a hostname of its own
    # arrives here with whole names and skips this.
    resolve = ""
    if IP_PLACEHOLDER in obj or IP_PLACEHOLDER in comp:
        obj, comp = obj.replace(IP_PLACEHOLDER, "${DASHED}"), comp.replace(IP_PLACEHOLDER, "${DASHED}")
        if not public_address_command.strip():
            msg = "Names derived from the machine's address need the command that prints its public address"
            raise InvalidBootInput(msg)
        resolve = f'PUBIP=$({public_address_command.strip()})\nDASHED=$(echo "$PUBIP" | tr . -)\n'
    elif wait_for_dns and not behind_load_balancer and public_address_command.strip():
        resolve = _dns_wait_block((obj, comp), public_address_command)
    caddyfile = (
        f"{obj} {{\n    reverse_proxy 127.0.0.1:{OBJECT_SERVICE_PORT}\n}}\n"
        f"{comp} {{\n    reverse_proxy 127.0.0.1:{COMPUTE_SERVICE_PORT}\n}}\n"
    )
    backend_cert = ""
    if behind_load_balancer:
        # Only the load balancer reaches this port. The public certificate belongs to the load balancer,
        # so Caddy never asks for one (explicit HTTP, or automatic HTTPS off). Caddy preserves HTTPS
        # forwarded by the load balancer.
        site = ":80 {\n"
        if backend_tls:
            # HTTPS from the load balancer, which does not verify a target's certificate and sends no SNI,
            # so one self-signed certificate made here serves every name and the health check alike; it
            # never leaves the private network, hence ten years and no renewal.
            site = "{\n    auto_https off\n}\n:443 {\n    tls /etc/caddy/backend/cert.pem /etc/caddy/backend/key.pem\n"
            backend_cert = (
                "install -d -m 0750 -o root -g caddy /etc/caddy/backend\n"
                "openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=3lc-backend "
                "-keyout /etc/caddy/backend/key.pem -out /etc/caddy/backend/cert.pem 2>/dev/null\n"
                "chgrp caddy /etc/caddy/backend/key.pem && chmod 0640 /etc/caddy/backend/key.pem\n"
            )
        caddyfile = (
            site + f"    @health path /__3lc_health\n    respond @health 200\n"
            f"    @object host {obj}\n    handle @object {{\n"
            f"        reverse_proxy 127.0.0.1:{OBJECT_SERVICE_PORT} {{\n"
            f"            header_up X-Forwarded-Proto https\n        }}\n    }}\n"
            f"    @compute host {comp}\n    handle @compute {{\n"
            f"        reverse_proxy 127.0.0.1:{COMPUTE_SERVICE_PORT} {{\n"
            f"            header_up X-Forwarded-Proto https\n        }}\n    }}\n"
            f"    respond 404\n}}\n"
        )
    # Unquoted heredoc delimiter on purpose: ${DASHED} must expand; the Caddyfile has no other $ (hostnames only).
    return f"""timeout 600 $APT install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null 2>&1 || true
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg || true
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list || true
timeout 240 $APT update -y >/dev/null 2>&1 || true
timeout 600 $APT install -y caddy >/dev/null 2>&1 || true
if command -v caddy >/dev/null 2>&1; then
{resolve}{backend_cert}cat > /etc/caddy/Caddyfile <<__3LC_CADDY__
{caddyfile}__3LC_CADDY__
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy
echo "tls=ok" >> {BOOT_STATUS_FILE}
else
echo "tls=failed: caddy did not install; the services answer on ports 5015 and 5020 from this instance only (the security group opens 80 and 443)" >> {BOOT_STATUS_FILE}
fi
"""
