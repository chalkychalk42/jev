# Visual teacher transport

## Current choice: Claude subscription, `claude-opus-4-7` at medium effort (23 September 2026)

The owner dropped GLM for the tutor after five teach sessions in which the free
`glm-4.6v-flash` tier refused most requests ("overloaded", 1305), and chose the Claude
subscription with a medium-effort model. The launch configuration passes
`--teacher-provider claude --teacher-model claude-opus-4-7 --teacher-effort medium` and the
native `claude.exe`; `--teacher-effort` maps to the CLI's `--effort` (low, medium, high,
xhigh, max; Claude Code 2.1.280) and is recorded in `play-config.json` and in every reply's
model label (`claude-sub:claude-opus-4-7@medium`).

Measured on native Windows with saved frames (no game input): the preflight reported
subscription authentication and every required flag; one smoke reply took 5.7 s; a
non-executing replay of the Echo Ridge kobold camp returned `select_unit` on the Kobold
Worker's nameplate in 6.4 s, well inside the 30 s decision deadline. These calls draw on
the owner's subscription allocation, which is also what their own Claude Code sessions use.

## GLM API option

The visual tutor can also use the explicit `--teacher-provider glm` transport. This was
added at the owner's request after Claude's subscription quota blocked live verification.
It uses the same observation, bounded-action validation, executor, budget and learning
records; choosing a provider does not change game controls or outcome rules.

The default GLM model is `glm-4.6v-flash`, an image-capable model listed as free on the
[official pricing page](https://docs.z.ai/guides/overview/pricing) when checked on
22 September 2026. It makes one bounded API attempt with thinking disabled and a
1,024-token output ceiling. There is no automatic paid-model fallback or retry. Model
identity and actual input/output token usage are recorded when the API supplies them.

Credentials come from the named `GLM_API_KEY` environment variable, or the ignored local
`.env` file when the environment does not define it. `--teacher-key-env` and
`--teacher-env-file` configure those sources without placing a key in launch arguments,
run configuration or Git. The only accepted endpoints are the official Z.AI and BigModel
HTTPS APIs; redirects are refused. The endpoint must match the key's issuing platform.

```text
python tools/check_teaching.py --teacher-provider glm --transport-check
python tools/check_teaching.py --teacher-provider glm --smoke-image SAVED.png --output captures/teaching/glm-smoke.json
python tools/probe_slice.py --play-mode teach --teacher-provider glm --route-mode supported --screenshots --run-for 60 --reconnect
```

Z.AI uses `https://api.z.ai/api/paas/v4`; BigModel uses
`--teacher-base-url https://open.bigmodel.cn/api/paas/v4`. The smoke command sends one
saved image but cannot send game input. The normal default check makes no API request.

In `teach` mode, the model chooses every action, including requests to run existing
skills. It is not called for each screenshot or radio tick. In `adaptive` mode, a
qualified local capability can execute covered actions; novel situations and teacher
audits still require a model. A new installation has no proven student to replace it.

### Reply contract and replay evidence, 23 September 2026

The tutor now answers with one action name from a menu built from the current state, plus
that action's flat parameters (`jev/play/tutor.py`, DECISIONS V43). Capability and expected
effect are derived locally. The prompt is compact text: goal, character, target, nameplate
detections, UI, bags, the measured results of recent actions, and the available actions.
Transports only transport; `VisionTeacher` validates every provider's reply once.

Non-executing replays of saved frames through `glm-4.6v-flash`
(`tools/check_teaching.py --replay-image FRAME --replay-step STEP --replay-skill SKILL`):
the login dialog returned `escape` (2,911 input tokens), the wolf-beside frame returned
`skill:COMBAT_PROFILE` (3,624 / 67 tokens, 3.8 s), and the wolf-to-the-right frame met three
consecutive "overloaded" responses (business code 1305). Transient codes 1302/1305 and 5xx
are retried with 2/4/6 s backoff inside the decision deadline; an exhausted balance (1113)
is not. A tutor that cannot answer hands the objective to the scripted routine.

A text-only probe on 23 September showed the supplied key belongs to a GLM Coding Plan.
`glm-4.6v` answers on `https://api.z.ai/api/coding/paas/v4`, but the plan's FAQ restricts
it to supported coding tools, so the bot does not use that endpoint. On the pay-as-you-go
endpoint only the free Flash model is available; paid vision models return 1113 until the
account has balance (GLM-4.6V-FlashX is listed at $0.04/$0.40 per million tokens).

### GLM connection evidence, 22 September 2026

Native Windows returned a validated reply from `glm-4.6v-flash` in **2.233 seconds**
using a generated 64×64 image and synthetic literals. It correctly identified the red,
green and blue shapes; API usage was 1,437 input and 84 output tokens. No gameplay data
was supplied and no action was executed. Local preflight and provider/factory/CLI tests
also pass. This confirms the supplied key works at the Z.AI endpoint, independently of
Claude's quota; it does not establish game-playing competence.

Automatic approval review initially rejected the separate saved-game-image smoke request
because explicit approval for gameplay images/state going to Z.AI had not been established.
The owner subsequently confirmed the full supervised test, resolving that export block.
A saved gameplay frame then returned a valid observe reply describing the visible wolf
in 4.407 seconds, with 5,632 input and 114 output tokens. This probe executed no input.
The independently authorized local reconnect test succeeded with 21 reviewed one-second
frames, no missing/skipped frames, and all inputs physically released afterward.

### Supervised acceptance attempt, 22 September 2026

The approved 180-second teaching session `20260922T211913-c2c0e3` stopped after about
10 seconds: its first model proposal failed the bounded reply contract. No action was
accepted or executed. All 12 captured frames (11 periodic, one event) were reviewed;
there were no missing/skipped frames and the largest periodic interval was 1.008 seconds.
The blocking modal remained visible; wolf-meat progress stayed 0/8.

Saved-scene diagnostics isolated an unsupported UI control, then a valid Escape action
with the invalid purpose label `capability="key"`. The shared schema now exposes only
observation and Escape while a modal is observed, matching the executor's existing rule.
Purpose labels and tap durations are documented explicitly. The model-facing prompt
omits the local student's numerical image descriptor and references repeated provenance
once, preserving the full PNG, game state and all 156 configured binding rows. Original
recordings and fingerprints are unchanged. Comparable modal requests dropped from
28,080 to 15,877 input tokens, but Flash still failed reply validation.

Eight non-executing saved-scene diagnostic/comparison calls followed the failed live
attempt. The explicit `glm-4.6v` comparison returned HTTP 429, without a served model or
token usage; this does not establish the cause of the provider limit. Native function
calling and enabled-thinking experiments also failed action validation. Neither
experiment changed production transport. Invalid replies were never coerced into
actions, and no repeated live input loop was started.

The final read-only check showed character selection and all keys/buttons released.
The supervised acceptance is **blocked by tutor reply compliance**, not passed. Approach,
damage, death/loot, quest progress and useful motor learning remain unverified. Claude
was not called in this attempt. Local evidence includes the run's `supervised-review.json`
and `captures/teaching/glm-*-replay.json` / `glm-*-experiment.json` files.

## Claude subscription option

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
