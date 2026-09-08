# ROGUEGUI

ROGUEGUI is the controlled GUI-agent attack benchmark used in the Argus
evaluation. This repository contains the paper-aligned 73-case release and the
Attack Lab Android application that implements those cases.

## Included cases

| Category | Cases |
|---|---:|
| Hallucination | 10 |
| Direct Prompt Injection | 15 |
| UI Representation Manipulation | 30 |
| Temporal Binding | 8 |
| GUI Hijacking | 10 |
| **Total** | **73** |

## Attack categories

- **Hallucination Exploitation.** Ambiguous or underspecified instructions
  exploit the LLM's tendency to infer nonexistent intents, UI targets, or
  action paths. The resulting mistake may cause an unsafe GUI action, such as
  deleting data or triggering another destructive operation
  [[1]](#ref-1).
- **Direct Prompt Injection.** Malicious instructions are appended to the
  user's task to override its benign intent or alter the agent's action
  selection. ROGUEGUI includes detailed-ignore, completion-style, combined,
  Tree-of-Attacks with Pruning, and HackAPrompt-style strategies
  [[2]](#ref-2).
- **UI Representation Manipulation.** GUI agents may observe screenshots,
  accessibility trees, or XML hierarchies, and these channels can disagree.
  ROGUEGUI tests deceptive dialogs, notifications, and advertisement-like
  screen elements, as well as incorrect XML tags and malicious instructions
  hidden in deeply nested structures. An attacker can therefore distort one
  or both observation channels and redirect the agent
  [[3]](#ref-3) [[4]](#ref-4) [[5]](#ref-5) [[6]](#ref-6).
- **Temporal Binding.** A GUI may change during the agent's
  observation-to-action interval. ROGUEGUI replaces a UI element inside this
  window so that the eventual action reaches a different target from the one
  the agent analyzed, exercising the GUI time-of-check-to-time-of-use gap
  [[7]](#ref-7).
- **GUI Hijacking.** A GUI action exposes little semantic meaning by itself:
  a benign-looking element can invoke sensitive application logic. ROGUEGUI
  uses UI mimicry, tapjacking, overlay attacks, and task hijacking to divert
  the final action to a risky operation or trap element
  [[8]](#ref-8) [[3]](#ref-3) [[9]](#ref-9) [[10]](#ref-10).

Each JSON file preserves the case-level prompt, Android page and scenario ID,
dangerous-button label, and dangerous action. The dual-representation cases
also include `runtime_locator_label`: their screen text and accessibility text
are both deceptive, so `dangerous_button` remains the semantic action while
`runtime_locator_label` identifies the concrete accessibility node that owns
that dangerous callback. `Attack Lab APP/` contains the corresponding Android
source.

## Build and install Attack Lab

Requirements are JDK 17, Android SDK 34, and an emulator or device visible to
ADB.

```bash
cd "ROGUEGUI/Attack Lab APP"
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.mobicom.guibench/.MainActivity
```

The Gradle build uses the standard local Android SDK configuration. No machine-
specific `local.properties` file or signing key is included.

## References

References are numbered in order of first appearance in this README.

<a id="ref-1"></a>**[1]** L. Huang et al. “A Survey on Hallucination in
Large Language Models: Principles, Taxonomy, Challenges, and Open Questions.”
*ACM Transactions on Information Systems*, 43(2), 2025.

<a id="ref-2"></a>**[2]** Y. Liu, Y. Jia, R. Geng, J. Jia, and N. Z. Gong.
“Formalizing and Benchmarking Prompt Injection Attacks and Defenses.” *USENIX
Security*, 2024.

<a id="ref-3"></a>**[3]** A. Bianchi, J. Corbetta, L. Invernizzi,
Y. Fratantonio, C. Kruegel, and G. Vigna. “What the App Is That? Deception and
Countermeasures in the Android User Interface.” *IEEE Symposium on Security
and Privacy*, 2015.

<a id="ref-4"></a>**[4]** C. Ren, P. Liu, and S. Zhu. “WindowGuard:
Systematic Protection of GUI Security in Android.” *NDSS*, 2017.

<a id="ref-5"></a>**[5]** G. Yang, J. Huang, and G. Gu. “Iframes/Popups Are
Dangerous in Mobile WebView: Studying and Mitigating Differential Context
Vulnerabilities.” *USENIX Security*, 2019.

<a id="ref-6"></a>**[6]** T. Zhang, C. Zhang, J. X. Morris, E. Bagdasarian,
and V. Shmatikov. “Self-Interpreting Adversarial Images.” *USENIX Security*,
2025.

<a id="ref-7"></a>**[7]** W. Xu. “Temporal UI State Inconsistency in Desktop
GUI Agents: Formalizing and Defending Against TOCTOU Attacks on Computer-Use
Agents.” *arXiv:2604.18860*, 2026.

<a id="ref-8"></a>**[8]** P. Beer, M. Squarcina, S. Roth, and M. Lindorfer.
“TapTrap: Animation-Driven Tapjacking on Android.” *USENIX Security*, 2025.

<a id="ref-9"></a>**[9]** C. Ren, Y. Zhang, H. Xue, T. Wei, and P. Liu.
“Towards Discovering and Understanding Task Hijacking in Android.” *USENIX
Security*, 2015.

<a id="ref-10"></a>**[10]** H. Zhou, S. Wu, C. Qian, X. Luo, H. Cai, and
C. Zhang. “Beyond the Surface: Uncovering the Unprotected Components of
Android Against Overlay Attack.” *NDSS*, 2024.
