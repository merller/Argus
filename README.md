# Argus

Argus is a system-level mandatory access-control mechanism for Android GUI
agents. This release contains the Argus policy backend and runtime artifacts,
along with the ROGUEGUI benchmark. Third-party agents, baselines, and public
datasets are referenced through upstream links and are not redistributed here.

## Repository layout

- `Argus/`: the kernel policy table, ART policy client, integration patch,
  tests, and deployable runtime overlay. Start with
  `Argus/README.md`.
- `ROGUEGUI/`: the paper-aligned 73-case GUI-agent security benchmark, its
  metadata, and the Attack Lab Android application.

## Argus artifact

The compact implementation files are under `Argus/art/`, `Argus/kernel/`, and
`Argus/patches/`. Policy manifests are under `Argus/policies/`. Runtime
deployment is described in `Argus/images/README.md`.

## ROGUEGUI benchmark

ROGUEGUI contains 73 cases: 15 Direct Prompt Injection, 10 GUI Hijacking, 10
Hallucination, 8 Temporal Binding, and 30 UI Representation Manipulation.
Case definitions and Attack Lab build instructions are in `ROGUEGUI/README.md`.

## External baselines

- [OS-Sentinel](https://github.com/OS-Copilot/OS-Sentinel): hybrid rule- and
  vision-language-model validation of a proposed mobile GUI action.
- [CSAgent](https://arxiv.org/abs/2509.22256): intent- and context-aware access
  control that maps GUI targets to application handlers.
- [AgentSentinel](https://arxiv.org/abs/2509.07764): system-event tracing and
  auditing for process, file, and network effects caused by agents.

No third-party baseline implementation is included in this repository.

## External datasets

- [AndroidWorld](https://github.com/google-research/android_world): 116 tasks
  across 20 Android applications.
- [DroidTask](https://github.com/MobileLLM/AutoDroid#about-dataset): 158 tasks
  across 13 Android applications, released by the AutoDroid authors.

The paper uses all 274 tasks from these two public datasets. Dataset source and
data must be obtained from the upstream projects and are not copied here.

## Mobile agents used in the evaluation

- [MobileGPT](https://github.com/mobilegptsys/MobileGPT): install its Python
  server dependencies, configure credentials locally, start `Server/main.py`,
  build and install the Android client, set its server address, and enable its
  Accessibility Service.
- [AppAgent](https://github.com/TencentQQGYLab/AppAgent): connect the emulator
  through ADB, install the Python requirements, configure `config.yaml`, run
  `learn.py` for app exploration, and run `run.py` for task deployment.
- [AutoDroid](https://github.com/MobileLLM/AutoDroid): install Python, Java,
  the Android SDK, and the upstream package; connect the emulator through ADB
  and launch a task with the upstream `droidbot` command.

The exact paper-aligned model settings and concise deployment commands are in
`Argus/README.md`. 
