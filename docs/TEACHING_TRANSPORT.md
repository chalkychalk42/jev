# Visual teacher transport

The visual tutor uses the existing Claude subscription through the native Claude Code
CLI. `ClaudeVisionClient` sends an actual base64 PNG image block and observation/control
JSON through `--input-format stream-json`; it requires a final successful result with a
`structured_output` object. It validates the bounded action schema and observation ID
before returning an action. The game executor independently verifies current state.

Built-in tools, MCP servers, customizations and session persistence are disabled. There
is no shell, file-reading tool, game API, API key or WSL bridge in the model's interface.
The image is supplied through stdin rather than a filename for the model to open.

## Deployment checked on 2026-09-22

| Environment | Python | Resolved Claude executable | CLI version |
| --- | --- | --- | --- |
| Production Windows | `C:\forever-win\Scripts\python.exe` | `C:\Users\NAS\.local\bin\claude.EXE` | `2.1.280` |
| Development WSL | `/home/ash/ForeverV2/.venv/bin/python` | `/home/ash/.local/bin/claude` | `2.1.280` |

Both environments passed version/help checks and reported authenticated Claude
subscription access (`claude.ai`, first-party provider). Only authentication booleans and
fixed provider labels were retained; no credentials or account identifiers were printed.
Production Windows also successfully launched `claude --version` using Python's actual
`asyncio.create_subprocess_exec` path. No game process was opened or controlled.

The installed CLI advertises every flag used by the visual transport, including image
stream input, stream output, schema output, tool disabling and permission-prompt
disabling. The native Windows executable is already on PATH. An explicit executable can
be supplied with `--teacher-binary 'C:\Users\NAS\.local\bin\claude.exe'`.
Windows `.cmd`/`.bat` wrappers are rejected with a clear instruction to use the native
executable; prompts are never routed through `cmd.exe`.

## Repeat the preflight without a model call

Run from the checkout. In WSL:

```bash
.venv/bin/python -c 'import json; from jev.play.teacher import ClaudeVisionClient; print(json.dumps(ClaudeVisionClient().preflight(), indent=2))'
```

In Windows PowerShell, from the same checkout:

```powershell
& 'C:\forever-win\Scripts\python.exe' -c "import json; from jev.play.teacher import ClaudeVisionClient; print(json.dumps(ClaudeVisionClient().preflight(), indent=2))"
```

Expected: `ok: true`, all `required_flags: true`, `authenticated: true`,
`subscription_auth: true`, and `model_call_made: false`. Failures name the unavailable
capability; authentication command output is reduced rather than printed verbatim.

The reusable readiness command checks the generated guide, knowledge snapshot, controls,
schemas and required Python dependencies without opening the game:

```bash
.venv/bin/python tools/check_teaching.py --transport-check --output captures/teaching/readiness.json
```

`--bindings PATH` can be repeated for account then character configuration. `--graph`,
`--world-db`, `--teacher-binary` and `--teacher-model` select explicit inputs. The world
snapshot defaults to `data/knowledge/tbc-243.sqlite`; its availability, exact-server
provenance and hash are reported, with missing optional facts remaining unknown. Without
`--transport-check`, the command performs only local offline checks.

The optional `--smoke-image PATH --timeout 30` sends exactly one subscription request
with an original saved PNG and decoded radio values. Its output schema permits only
`observe` with zero wait; it has no client attachment, capture, executor or HID path.
It retains image hash, requested/actual model, token usage, latency and structured reply
in `--output`. Knowledge lookups and automatic repeat calls are disabled for this probe.

On 2026-09-22 the automatic approval reviewer rejected this saved-game-image export
before execution because specific approval to send the screenshot and derived data to
Claude had not been established. **No stored game image was sent.** Local and native
Windows deployment checks passed independently.

A separate safer probe, `tools/check_synthetic_transport.py`, was approved: it generated
64×64 colored rectangles in memory and sent only synthetic public-example literals from
a neutral working directory. Its single native request failed transport/schema
qualification after 1.619 seconds, with no actual model or token usage reported. The
limited result is in `captures/teaching/synthetic-transport.json`; no automatic retry ran.
One subsequently authorized diagnostic request using the same synthetic-only class of
payload identified the provider response: **HTTP 429, weekly subscription limit**,
reported only: “You've hit your weekly limit · resets 4am (Europe/London)”. No reset date
is inferred. Its redacted evidence is in
`captures/teaching/synthetic-diagnostic.json`. The native response uses
`subtype: success` together with `is_error: true`; production transport correctly
classifies this as a failure and returns no action. No actual model or token usage was
reported, and no further request was attempted.

The vision round-trip remains unverified until subscription capacity is available.
Login and flag checks cannot establish available quota. Both synthetic probes were
independent of all game files, observations, local knowledge and user content.

Preflight does **not** establish that an image/schema request has completed through the
subscription, that the requested model was used, or that the tutor plays successfully.
The separate bounded smoke test must use a stored screenshot and record the actual
model, usage, latency and typed reply. The prior strategic teacher requested Sonnet while
usage reported Haiku, so requested and actual model identity remain separate fields.
Live action success and learning promotion require their own observed evidence.

Transport protocol references:
[streaming image input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
[structured output](https://code.claude.com/docs/en/headless#get-structured-output).
