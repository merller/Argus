# Android Guard Defenses

This repo now exposes the same defense switch across the three agents:

- `AppAgent`
- `AutoDroid`
- `MobileGPT`

All three agents use one explicit method-name switch to select a defense baseline. Use exactly one of the following values:

| Config value | Defense baseline | Runtime requirement |
| --- | --- | --- |
| `asb` | ASB | Text LLM API |
| `delimiters` | Delimiters prompt baseline | Text LLM API |
| `temporal_pusv` | PUSV | UI XML/screenshot; no LLM required for the online surrogate |
| `task_shield` | Task Shield | LLM API |
| `agrail` | AGrail | GPT-4o-compatible vision/text API |
| `kairos` | OS Kairos | OS-Atlas-Pro-7B local model or compatible vision API backend |
| `agent_sentinel` | AgentSentinel | Syscall trace plus text LLM API |
| `agentsight` | AgentSight | Syscall trace plus text LLM API |

The shared runtime entry is [android_guard_router.py](./android_guard_router.py). 

## Method Notes

ASB employs GPT-4o to augment user instructions with defensive prompt content.

AGrail leverages two collaborative off-the-shelf LLMs (GPT-4o) and a memory module to adaptively execute action-level safety checks on agents.

Task Shield implements a defense mechanism that employs GPT-4o as a supervisor LLM to verify whether each extracted instruction or tool call contributes to the user's specified objectives.

OS Kairos gates each action on a confidence-probing step, using OS-Atlas-Pro-7B as the probing model and GPT-4o as the critic model to assess alignment between the agent action and user intent.

AgentSight uses eBPF to correlate intercepted LLM traffic with kernel-level events, detecting deviations between an agent's intent and its actual actions via GPT-4o.

AgentSentinel instruments 16 system-level trace probes to capture and suspend sensitive operations triggered by tool execution, then submits to an external LLM for adaptive hybrid threat auditing.

PUSV performs pre-execution UI state verification by re-checking the interface immediately before action dispatch, combining local target-region consistency checks with broader screen-state comparisons to detect TOCTOU-style target swaps and malicious redirections.


## Selecting A Defense

The only method-specific value that changes between baseline runs is the method name above.

For example:

```powershell
$env:MOBILEGPT_GUARD_METHOD_NAME = "kairos"
```

means "run MobileGPT with the OS Kairos baseline." To run a different baseline, replace only the string value with another config value from the table, such as `"asb"`, `"task_shield"`, or `"agentsight"`.

Use the corresponding prefix for each agent:

| Agent | Method variable | Run/export variables |
| --- | --- | --- |
| AppAgent | `GUARD_METHOD_NAME` in `AppAgent/config.yaml` | `GUARD_EXPORT_ENABLED`, `GUARD_RUN_EXTERNAL` |
| AutoDroid | `AUTODROID_GUARD_METHOD_NAME` or `--guard-method` | `AUTODROID_GUARD_EXPORT_ENABLED`, `AUTODROID_GUARD_RUN_EXTERNAL` |
| MobileGPT | `MOBILEGPT_GUARD_METHOD_NAME` or `--guard-method` | `MOBILEGPT_GUARD_EXPORT_ENABLED`, `MOBILEGPT_GUARD_RUN_EXTERNAL` |

For `agent_sentinel` and `agentsight`, also enable the trace and run the guard after action execution so syscall evidence is attached.

## Common Environment

Set these once for model-based guards:

```powershell
$env:OPENAI_API_BASE = "https://your-provider/v1"
$env:OPENAI_API_KEY = "your-key"
$env:TEXT_MODEL = "gpt-4o-mini"
$env:VISION_MODEL = "gpt-4o"
```

Optional target hints:

```powershell
$env:GUARD_ALLOWED_PACKAGE = "com.example.app"
$env:GUARD_ALLOWED_ACTIVITY_PREFIX = "com.example.app/"
```

## AppAgent

Edit [AppAgent/config.yaml](./AppAgent/config.yaml):

```yaml
GUARD_EXPORT_ENABLED: true
GUARD_RUN_EXTERNAL: true
GUARD_METHOD_NAME: "asb"  # Replace with any value from the baseline table.
GUARD_SCRIPT_PATH: ""
```

Then run AppAgent normally. `GUARD_SCRIPT_PATH` can stay empty; the agent will use `android_guard_router.py`.

For Agent-Sentinel:

```yaml
GUARD_EXPORT_ENABLED: true
GUARD_RUN_EXTERNAL: true
GUARD_RUN_AFTER_ACTION: true
GUARD_METHOD_NAME: "agent_sentinel"
DEVICE_TRACE_ENABLED: true
```

For AgentSight, use the same trace settings and set:

```yaml
GUARD_METHOD_NAME: "agentsight"
```

## AutoDroid

Use command-line flags:

```powershell
python start.py -a path\to\app.apk -d emulator-5554 -task "your task" --guard-method asb --guard-run-external
```

Or use environment variables:

```powershell
$env:AUTODROID_GUARD_METHOD_NAME = "task_shield"
$env:AUTODROID_GUARD_EXPORT_ENABLED = "true"
$env:AUTODROID_GUARD_RUN_EXTERNAL = "true"
python start.py -a path\to\app.apk -d emulator-5554 -task "your task"
```

For Agent-Sentinel, also enable trace and run the guard after the action:

```powershell
$env:AUTODROID_GUARD_METHOD_NAME = "agent_sentinel"
$env:AUTODROID_GUARD_EXPORT_ENABLED = "true"
$env:AUTODROID_GUARD_RUN_EXTERNAL = "true"
$env:AUTODROID_GUARD_RUN_AFTER_ACTION = "true"
$env:AUTODROID_GUARD_TRACE_ENABLED = "true"
```

For AgentSight, set `AUTODROID_GUARD_METHOD_NAME` to `agentsight` and keep the same trace variables.

## MobileGPT

Use command-line flags:

```powershell
python Server\main.py --guard-method delimiters --guard-run-external
```

Or use environment variables:

```powershell
$env:MOBILEGPT_GUARD_METHOD_NAME = "kairos"
$env:MOBILEGPT_GUARD_EXPORT_ENABLED = "true"
$env:MOBILEGPT_GUARD_RUN_EXTERNAL = "true"
python Server\main.py
```


For Agent-Sentinel:

```powershell
$env:MOBILEGPT_GUARD_METHOD_NAME = "agent_sentinel"
$env:MOBILEGPT_GUARD_EXPORT_ENABLED = "true"
$env:MOBILEGPT_GUARD_RUN_EXTERNAL = "true"
$env:MOBILEGPT_GUARD_RUN_AFTER_ACTION = "true"
$env:MOBILEGPT_GUARD_TRACE_ENABLED = "true"
```

For AgentSight, set `MOBILEGPT_GUARD_METHOD_NAME` to `agentsight` and keep the same trace variables.
