# Argus

Argus is a research artifact for studying and defending Android GUI agents. The repository contains the Android-side Argus prototype, the ROGUEGUI benchmark, and adapted GUI-agent/baseline code used to evaluate agent behavior under GUI-level attacks.

This README focuses on the code artifact. For the full design rationale and evaluation methodology, please refer to the paper.

## Repository Contents

```text
.
├── Argus/
│   ├── frameworks/        # Android framework and native input-stack changes
│   ├── art/               # ART/JNI-side support code
│   └── kernel/            # Kernel-side DCI policy table prototype
├── ROGUEGUI/
│   ├── Attack Lab APP/    # Android benchmark app for controlled GUI scenarios
│   ├── Hallucination/
│   ├── Direct Prompt Injection/
│   ├── UI representation manipulation/
│   ├── Temporal binding/
│   ├── GUI Hijacking/
│   ├── Backdoor/
│   ├── Tool-chain Tampering/
│   └── AgentDojo GUI Adaptation/
└── Agents & Baselines/
    ├── AppAgent/
    ├── AutoDroid/
    ├── MobileGPT-main/
    └── README_guard_defenses.md
```

## Components

### `Argus/`

`Argus/` contains the Android platform modifications for the Argus prototype. The files are organized according to their corresponding AOSP locations so that the changes can be inspected or ported into an Android 14 source tree.

The implementation includes support for:

- tagging GUI-agent-originated input events in the Android input path;
- resolving GUI actions to target view/callback context;
- constructing DCI-style identifiers for policy lookup;
- applying policy verdicts such as allow, block, and restricted confirmation;
- gating agent input while a user-facing confirmation decision is pending.

Important files include Android framework classes under `frameworks/base/core/java/android/view/`, native input changes under `frameworks/native/`, ART/JNI support under `art/runtime/jni/`, and the prototype policy table in `kernel/argus_dci_policy_table.c`.

### `ROGUEGUI/`

`ROGUEGUI/` is the benchmark suite for evaluating GUI agents under security-relevant Android GUI scenarios. It contains 103 JSON case descriptions together with an Android Attack Lab app that hosts the controlled pages, widgets, and task flows referenced by those cases.

The benchmark is organized by attack category:

| Category                          | Cases | Contents                                                     |
| --------------------------------- | ----: | ------------------------------------------------------------ |
| `UI representation manipulation/` |    30 | Cases where screenshots, XML trees, or both expose misleading or inconsistent UI information. |
| `Direct Prompt Injection/`        |    15 | Prompt-injection cases embedded in GUI-facing task content.  |
| `GUI Hijacking/`                  |    15 | Mimicry, overlay, tapjacking, task-hijacking, and related semantic-redirection scenarios. |
| `Backdoor/`                       |    10 | Agent-side trigger scenarios with benign user prompts and unauthorized GUI outcomes. |
| `Hallucination/`                  |    10 | Cases that test whether an agent invents unsupported actions, paths, settings, or workflow steps. |
| `Tool-chain Tampering/`           |     9 | Tool metadata poisoning, tool output pollution, and cross-tool control-flow hijacking cases. |
| `Temporal binding/`               |     8 | GUI time-of-check/time-of-use cases where the observed state and acted-on state may diverge. |
| `AgentDojo GUI Adaptation/`       |     6 | GUI adaptations of task-oriented security scenarios inspired by AgentDojo-style workflows. |

Each JSON file acts as benchmark metadata: it names the scenario, describes the user-facing task, and records the information needed to evaluate whether the agent completed the intended action or triggered an unauthorized outcome. The files are kept separate from agent implementations so the same cases can be reused across AppAgent, AutoDroid, MobileGPT, Argus, and baseline defenses.

`ROGUEGUI/Attack Lab APP/` contains the Android app backing many of the synthetic GUI scenarios. Its package name is `com.mobicom.guibench`, and its source code defines the scenario activities, deceptive widgets, task-hijacking pages, temporal cases, and logging markers used by the benchmark. Category-specific notes may appear inside individual subdirectories, for example `ROGUEGUI/Backdoor/README.md`.

ROGUEGUI is intended to be a controlled benchmark artifact rather than a collection of real-world exploit scripts. The cases use synthetic pages, mock actions, and explicit success/violation markers so defenses and agents can be compared consistently.

### `Agents & Baselines/`

`Agents & Baselines/` contains the GUI agents and defense baselines used with the benchmark:

- `AppAgent/`
- `AutoDroid/`
- `MobileGPT-main/`

The shared baseline-defense notes are documented in `Agents & Baselines/README_guard_defenses.md`. The agent folders retain their own upstream-style README files where applicable, while the added guard adapters and tracing hooks support a common evaluation interface across agents.

## Artifact Scope

This repository is intended for research on GUI-agent security and system-level enforcement. The attack scenarios are designed for controlled emulator or test-device environments using synthetic apps, mock tasks, and benchmark prompts. They should not be used against real users, production accounts, or third-party services.

The repository deliberately separates three concerns:

- **Platform enforcement:** Android-side Argus modifications under `Argus/`.
- **Benchmark data:** ROGUEGUI cases and the Attack Lab app under `ROGUEGUI/`.
- **Agent evaluation:** adapted agents, guard adapters, and baseline defenses under `Agents & Baselines/`.

## Notes

- Some subdirectories include code adapted from existing GUI-agent projects and keep their original README/license files.
- Benchmark JSON files are data artifacts; changing them may affect reported counts and evaluation comparability.
- The Android platform files are provided as source-level artifacts rather than as a prebuilt system image.
