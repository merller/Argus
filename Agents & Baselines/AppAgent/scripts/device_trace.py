import os
import re
import shlex
import subprocess
import tempfile
import time

from utils import print_with_color


_adb_root_state = {}
_run_as_state = {}
_wrap_runtime_state = {}


def _sanitize_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "unknown")


def _unique_keep_order(values):
    seen = set()
    ordered = []
    for value in values:
        if value and value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def _adb_base(configs, device):
    adb_path = configs.get("ADB_PATH", "adb")
    cmd = [adb_path]
    if device:
        cmd.extend(["-s", device])
    return cmd


def _trace_cmd_timeout(configs):
    try:
        return float(configs.get("DEVICE_TRACE_CMD_TIMEOUT", 12))
    except Exception:
        return 12.0


def _trace_mode(configs):
    mode = str(configs.get("DEVICE_TRACE_MODE", "attach") or "attach").strip().lower()
    if mode not in ["attach", "wrap"]:
        return "attach"
    return mode


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_host_cmd(cmd, timeout_sec=None):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            stdout=_as_text(result.stdout),
            stderr=_as_text(result.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            124,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr) + f"\nTIMEOUT after {timeout_sec}s",
        )


def _ensure_adb_root(configs, device):
    cache_key = (configs.get("ADB_PATH", "adb"), device)
    if cache_key in _adb_root_state:
        return _adb_root_state[cache_key]

    if not device or not configs.get("DEVICE_TRACE_USE_ADB_ROOT", True):
        _adb_root_state[cache_key] = False
        return False

    timeout_sec = _trace_cmd_timeout(configs)
    root_result = _run_host_cmd(_adb_base(configs, device) + ["root"], timeout_sec=timeout_sec)
    combined_output = "%s\n%s" % (root_result.stdout or "", root_result.stderr or "")
    normalized_output = combined_output.lower()
    root_requested = (
        root_result.returncode == 0
        and (
            "restarting adbd as root" in normalized_output
            or "adbd is already running as root" in normalized_output
        )
    )
    if not root_requested:
        _adb_root_state[cache_key] = False
        return False

    _run_host_cmd(_adb_base(configs, device) + ["wait-for-device"], timeout_sec=max(timeout_sec, 20))
    time.sleep(1.0)
    id_result = _run_host_cmd(_adb_base(configs, device) + ["shell", "id"], timeout_sec=timeout_sec)
    adb_root_ready = id_result.returncode == 0 and "uid=0" in (id_result.stdout or "")
    _adb_root_state[cache_key] = adb_root_ready
    return adb_root_ready


def _can_run_as_package(configs, device, package_name):
    cache_key = (configs.get("ADB_PATH", "adb"), device, package_name)
    if cache_key in _run_as_state:
        return _run_as_state[cache_key]

    if not device or not package_name or not configs.get("DEVICE_TRACE_USE_RUN_AS", True):
        _run_as_state[cache_key] = False
        return False

    timeout_sec = _trace_cmd_timeout(configs)
    result = _run_host_cmd(
        _adb_base(configs, device) + ["shell", "run-as %s id" % shlex.quote(package_name)],
        timeout_sec=timeout_sec,
    )
    available = result.returncode == 0 and "uid=" in (result.stdout or "")
    _run_as_state[cache_key] = available
    return available


def _run_device_shell_run_as(configs, device, package_name, shell_command):
    shell_command = str(shell_command or "")
    quoted_shell_command = shlex.quote(shell_command)
    timeout_sec = _trace_cmd_timeout(configs)
    cmd = _adb_base(configs, device) + [
        "shell",
        "run-as %s sh -c %s" % (shlex.quote(package_name), quoted_shell_command),
    ]
    return _run_host_cmd(cmd, timeout_sec=timeout_sec)


def _pull_run_as_file(configs, device, package_name, remote_file, local_trace_dir):
    local_file = os.path.abspath(os.path.join(local_trace_dir, os.path.basename(remote_file)))
    timeout_sec = _trace_cmd_timeout(configs)
    cmd = _adb_base(configs, device) + [
        "exec-out",
        "run-as",
        package_name,
        "sh",
        "-c",
        "cat %s" % shlex.quote(remote_file),
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        return local_file, subprocess.CompletedProcess(
            cmd,
            124,
            stdout=exc.stdout or b"",
            stderr=(exc.stderr or b"") + ("\nTIMEOUT after %ss" % timeout_sec).encode("utf-8"),
        )

    if result.returncode == 0:
        with open(local_file, "wb") as outfile:
            outfile.write(result.stdout or b"")

    return local_file, subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=_as_text(result.stdout),
        stderr=_as_text(result.stderr),
    )


def _run_device_shell(configs, device, shell_command, require_root=False, fallback_to_non_root=True):
    shell_command = str(shell_command or "")
    quoted_shell_command = shlex.quote(shell_command)

    variants = []
    if require_root and _ensure_adb_root(configs, device):
        variants.append(_adb_base(configs, device) + ["shell", "sh -c %s" % quoted_shell_command])
    if require_root and configs.get("DEVICE_TRACE_USE_SU", True):
        variants.append(_adb_base(configs, device) + ["shell", "su 0 sh -c %s" % quoted_shell_command])
    if not require_root or fallback_to_non_root or not variants:
        variants.append(_adb_base(configs, device) + ["shell", "sh -c %s" % quoted_shell_command])

    first_result = None
    last_result = None
    timeout_sec = _trace_cmd_timeout(configs)
    for cmd in variants:
        result = _run_host_cmd(cmd, timeout_sec=timeout_sec)
        if first_result is None:
            first_result = result
        last_result = result
        if result.returncode == 0:
            return result
    return first_result or last_result


def _remote_glob_expr(remote_prefix):
    normalized = (remote_prefix or "").rstrip("/")
    remote_dir = os.path.dirname(normalized) or "."
    remote_name = os.path.basename(normalized)
    return "%s/%s*" % (shlex.quote(remote_dir), shlex.quote(remote_name))


def _list_remote_trace_files_run_as(configs, device, package_name, remote_prefix):
    glob_expr = _remote_glob_expr(remote_prefix)
    command = (
        "for path in {glob_expr}; do "
        "if [ -f \"$path\" ]; then printf '%s\\n' \"$path\"; fi; "
        "done"
    ).format(glob_expr=glob_expr)
    result = _run_device_shell_run_as(configs, device, package_name, command)
    if result is None or result.returncode != 0:
        return []
    return sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])


def _list_remote_trace_files(configs, device, remote_prefix):
    glob_expr = _remote_glob_expr(remote_prefix)
    command = (
        "for path in {glob_expr}; do "
        "if [ -f \"$path\" ]; then printf '%s\\n' \"$path\"; fi; "
        "done"
    ).format(glob_expr=glob_expr)
    result = _run_device_shell(configs, device, command, require_root=True)
    if result is None or result.returncode != 0:
        return []
    return sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])


def _read_remote_text(configs, device, remote_path, require_root=False):
    if not remote_path:
        return ""
    result = _run_device_shell(
        configs,
        device,
        "cat %s 2>/dev/null" % shlex.quote(remote_path),
        require_root=require_root,
    )
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def _read_remote_text_run_as(configs, device, package_name, remote_path):
    if not remote_path:
        return ""
    result = _run_device_shell_run_as(
        configs,
        device,
        package_name,
        "cat %s 2>/dev/null" % shlex.quote(remote_path),
    )
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def _trace_sidecar_paths(remote_prefix):
    normalized = (remote_prefix or "").rstrip("/")
    remote_dir = os.path.dirname(normalized) or "."
    remote_name = os.path.basename(normalized)
    meta_prefix = "%s/.appagent_trace_meta_%s" % (remote_dir, _sanitize_name(remote_name))
    return {
        "stderr": meta_prefix + ".stderr",
    }


def _run_as_trace_dir(package_name):
    return "/data/user/0/%s/files/appagent_guard" % package_name


def _build_trace_filter_candidates(trace_filter):
    requested = (trace_filter or "").strip()
    candidates = []
    if requested:
        candidates.append(requested)

    legacy_subset = "execve,open,openat,rename,renameat,unlink,unlinkat,connect,listen,accept,clone,fork,vfork,kill,exit,exit_group"
    if requested != legacy_subset:
        candidates.append(legacy_subset)

    broad_categories = "%file,%network,%process"
    if requested != broad_categories:
        candidates.append(broad_categories)

    return _unique_keep_order(candidates)


def _trace_metadata(configs, mode=None):
    trace_tool = configs.get("DEVICE_TRACE_TOOL", "strace")
    trace_filter = configs.get(
        "DEVICE_TRACE_FILTER",
        "fork,vfork,clone,clone3,execve,execveat,kill,exit,exit_group,open,openat,openat2,unlink,unlinkat,rename,renameat,renameat2,connect,listen,accept,accept4",
    )
    resolved_mode = mode or _trace_mode(configs)
    if resolved_mode == "wrap":
        capture_method = "guest-side strace via wrap.<package> app launch (persistent trace with per-step snapshots)"
        semantics = "Syscalls issued by the traced Android app process inside the emulator guest OS, captured from a wrap-launched process."
    else:
        capture_method = "guest-side strace attach (-p PID)"
        semantics = "Syscalls issued by the traced Android app process inside the emulator guest OS."
    return {
        "trace_scope": "android_guest_syscalls_via_strace",
        "trace_capture_method": capture_method,
        "trace_semantics": semantics,
        "trace_mode": resolved_mode,
        "trace_tool": trace_tool,
        "trace_filter": trace_filter,
        "trace_filter_candidates": _build_trace_filter_candidates(trace_filter),
    }


def _unique_numeric_pids(values):
    seen = set()
    ordered = []
    for value in values:
        value = str(value).strip()
        if value.isdigit() and value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def _extract_pids_from_text(package_name, raw_text):
    if not raw_text:
        return []

    pids = []

    regex_patterns = [
        rf"(\d+):{re.escape(package_name)}(?:[/:][^\s}}]+)?",
        rf"pid=(\d+)\b.*?{re.escape(package_name)}",
        rf"\bProcessRecord\{{[^}}]*\b(\d+):{re.escape(package_name)}(?:[/:][^\s}}]+)?",
    ]
    for pattern in regex_patterns:
        pids.extend(re.findall(pattern, raw_text))

    for line in raw_text.splitlines():
        if package_name not in line:
            continue
        fields = line.strip().split()
        for field in fields:
            if field.isdigit():
                pids.append(field)
                break

    return _unique_numeric_pids(pids)


def _query_package_pids(configs, device, package_name):
    if not package_name:
        return []

    commands = [
        ("pidof %s" % shlex.quote(package_name), False),
        ("pidof -s %s" % shlex.quote(package_name), False),
        ("ps -A -o PID,NAME 2>/dev/null", False),
        ("ps -A -o PID,ARGS 2>/dev/null", False),
        ("ps -A 2>/dev/null", False),
        ("ps 2>/dev/null", False),
        ("dumpsys activity processes 2>/dev/null", False),
        ("dumpsys activity processes 2>/dev/null", True),
    ]

    for command, require_root in commands:
        result = _run_device_shell(configs, device, command, require_root=require_root)
        if result is None or result.returncode != 0:
            continue
        pids = _extract_pids_from_text(package_name, result.stdout)
        if pids:
            return pids

    return []


def _compact_text(text, max_len=220):
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def _extract_first_relevant_line(raw_text, regexes, package_name=""):
    if not raw_text:
        return ""

    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in regexes]
    prioritized = []
    fallback = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(regex.search(line) for regex in compiled):
            fallback.append(line)
            if package_name and package_name in line:
                prioritized.append(line)

    candidates = prioritized or fallback
    if not candidates:
        return ""
    return _compact_text(candidates[0])


def _filter_logcat_excerpt(package_name, raw_text, limit=6):
    if not raw_text:
        return []

    package_pattern = re.escape(package_name) if package_name else ""
    compiled = [
        re.compile(r"AndroidRuntime", re.IGNORECASE),
        re.compile(r"FATAL EXCEPTION", re.IGNORECASE),
        re.compile(r"\bANR\b", re.IGNORECASE),
        re.compile(r"Force finishing", re.IGNORECASE),
        re.compile(r"Shutting down VM", re.IGNORECASE),
    ]
    if package_pattern:
        compiled.extend(
            [
                re.compile(package_pattern, re.IGNORECASE),
                re.compile(r"Process:\s*%s" % package_pattern, re.IGNORECASE),
            ]
        )

    matched = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(regex.search(line) for regex in compiled):
            matched.append(_compact_text(line, max_len=180))

    if len(matched) > limit:
        matched = matched[-limit:]
    return matched


def _collect_launch_diagnostics(configs, device, package_name):
    pid_values = _query_package_pids(configs, device, package_name)

    activity_result = _run_device_shell(
        configs,
        device,
        "dumpsys activity activities 2>/dev/null",
        require_root=False,
    )
    activity_text = "" if activity_result is None else activity_result.stdout
    activity_line = _extract_first_relevant_line(
        activity_text,
        [r"\btopResumedActivity\b", r"\bmResumedActivity\b"],
        package_name=package_name,
    )

    window_result = _run_device_shell(
        configs,
        device,
        "dumpsys window windows 2>/dev/null",
        require_root=False,
    )
    window_text = "" if window_result is None else window_result.stdout
    window_line = _extract_first_relevant_line(
        window_text,
        [r"\bmCurrentFocus\b", r"\bmFocusedApp\b"],
        package_name=package_name,
    )

    logcat_result = _run_host_cmd(
        _adb_base(configs, device) + ["logcat", "-d", "-t", "160", "-v", "brief"],
        timeout_sec=max(_trace_cmd_timeout(configs), 20),
    )
    logcat_text = "%s\n%s" % (logcat_result.stdout or "", logcat_result.stderr or "")
    logcat_lines = _filter_logcat_excerpt(package_name, logcat_text)

    return {
        "pid_values": pid_values,
        "activity_line": activity_line,
        "window_line": window_line,
        "logcat_lines": logcat_lines,
    }


def _detect_foreground_package_and_activity(configs, device):
    package_name = ""
    activity_name = ""

    window_result = _run_device_shell(
        configs,
        device,
        "dumpsys window windows 2>/dev/null",
        require_root=False,
    )
    window_text = "" if window_result is None else (window_result.stdout or "")
    if window_text:
        match = re.search(r"mCurrentFocus=.*? ([A-Za-z0-9._$]+)/([A-Za-z0-9_.$]+)\}", window_text)
        if match:
            package_name = match.group(1)
            activity_name = f"{match.group(1)}/{match.group(2)}"

    if not package_name:
        activity_result = _run_device_shell(
            configs,
            device,
            "dumpsys activity activities 2>/dev/null",
            require_root=False,
        )
        activity_text = "" if activity_result is None else (activity_result.stdout or "")
        if activity_text:
            match = re.search(r"mResumedActivity:.*? ([A-Za-z0-9._$]+)/([A-Za-z0-9_.$]+) ", activity_text)
            if not match:
                match = re.search(r"topResumedActivity=.*? ([A-Za-z0-9._$]+)/([A-Za-z0-9_.$]+) ", activity_text)
            if match:
                package_name = match.group(1)
                activity_name = f"{match.group(1)}/{match.group(2)}"

    return package_name, activity_name


def _format_launch_diagnostics(diagnostics):
    if not diagnostics:
        return "no diagnostics"

    parts = []
    pid_values = diagnostics.get("pid_values") or []
    parts.append("pid=%s" % (",".join(pid_values) if pid_values else "(missing)"))

    activity_line = diagnostics.get("activity_line") or ""
    if activity_line:
        parts.append("activity=%s" % activity_line)

    window_line = diagnostics.get("window_line") or ""
    if window_line:
        parts.append("focus=%s" % window_line)

    logcat_lines = diagnostics.get("logcat_lines") or []
    if logcat_lines:
        parts.append("logcat=%s" % " || ".join(logcat_lines[-3:]))

    return "; ".join(parts)


def _launch_looks_suspicious(diagnostics, package_name):
    if not diagnostics:
        return True

    if not (diagnostics.get("pid_values") or []):
        return True

    activity_line = diagnostics.get("activity_line") or ""
    window_line = diagnostics.get("window_line") or ""
    if package_name and package_name not in activity_line and package_name not in window_line:
        return True

    return False


def resolve_trace_packages(configs, current_package="", target_package="", current_activity=""):
    packages = []

    configured = configs.get("DEVICE_TRACE_PACKAGES", "")
    if configured:
        packages.extend([item.strip() for item in configured.split(",") if item.strip()])

    if current_activity and "/" in current_activity:
        packages.append(current_activity.split("/", 1)[0])

    packages.extend([target_package, current_package])

    if configs.get("DEVICE_TRACE_INCLUDE_SYSTEMUI", False):
        packages.append("com.android.systemui")

    return _unique_keep_order(packages)


def _wrap_runtime_failure(configs, device, package_name, activity_name, message):
    runtime = {
        "key": (configs.get("ADB_PATH", "adb"), device, package_name),
        "ready": False,
        "package": package_name,
        "activity": activity_name,
        "start_failures": [
            {
                "package": package_name,
                "activity": activity_name,
                "stderr": message,
            }
        ],
        "prepared_at": int(time.time()),
    }
    _wrap_runtime_state[runtime["key"]] = runtime
    print_with_color(message, "red")
    return runtime


def _resolve_wrap_package(configs, packages):
    configured = str(configs.get("DEVICE_TRACE_WRAP_PACKAGE", "") or "").strip()
    if configured:
        return configured

    guard_allowed = str(configs.get("GUARD_ALLOWED_PACKAGE", "") or "").strip()
    if guard_allowed:
        return guard_allowed

    configured_trace_packages = str(configs.get("DEVICE_TRACE_PACKAGES", "") or "").strip()
    if configured_trace_packages:
        for item in configured_trace_packages.split(","):
            item = item.strip()
            if item:
                return item

    for package_name in _unique_keep_order(packages or []):
        if package_name:
            return package_name

    return ""


def _resolve_wrap_activity(configs, device="", package_name=""):
    configured_activity = str(configs.get("DEVICE_TRACE_WRAP_ACTIVITY", "") or "").strip()
    if configured_activity:
        return configured_activity

    live_package = ""
    live_activity = ""
    if device:
        live_package, live_activity = _detect_foreground_package_and_activity(configs, device)

    if package_name and live_package == package_name and live_activity:
        print_with_color(
            "Using foreground activity %s for wrap-mode tracing because DEVICE_TRACE_WRAP_ACTIVITY is unset."
            % live_activity,
            "yellow",
        )
        return live_activity

    return ""


def _resolve_wrap_activity_args(configs):
    return str(
        configs.get("DEVICE_TRACE_WRAP_ACTIVITY_ARGS", "")
        or configs.get("DEVICE_TRACE_WRAP_INTENT_ARGS", "")
        or ""
    ).strip()


def _build_wrap_launch_command(configs, activity_name):
    command = "am start -n %s" % shlex.quote(str(activity_name or "").strip())
    activity_args = _resolve_wrap_activity_args(configs)
    if activity_args:
        command = "%s %s" % (command, activity_args)
    return command


def _wrap_trace_paths(package_name):
    trace_dir = _run_as_trace_dir(package_name)
    return {
        "trace_dir": trace_dir,
        "trace_prefix": trace_dir.rstrip("/") + "/trace.",
        "wrap_log": trace_dir.rstrip("/") + "/wrap.log",
    }


def _collect_wrap_launcher_artifacts(configs, device, package_name, remote_trace_dir):
    status_files = _list_remote_trace_files_run_as(
        configs,
        device,
        package_name,
        remote_trace_dir.rstrip("/") + "/trace_status.log.",
    )
    stderr_files = _list_remote_trace_files_run_as(
        configs,
        device,
        package_name,
        remote_trace_dir.rstrip("/") + "/trace_launcher.err.",
    )
    stdout_files = _list_remote_trace_files_run_as(
        configs,
        device,
        package_name,
        remote_trace_dir.rstrip("/") + "/trace_stdout.log.",
    )

    newest_status = status_files[-1] if status_files else ""
    newest_stderr = stderr_files[-1] if stderr_files else ""
    newest_stdout = stdout_files[-1] if stdout_files else ""
    status_text = _read_remote_text_run_as(configs, device, package_name, newest_status)
    stderr_text = _read_remote_text_run_as(configs, device, package_name, newest_stderr)
    stdout_text = _read_remote_text_run_as(configs, device, package_name, newest_stdout)

    exit_code = None
    status_match = re.search(r"strace_exit=(\d+)", status_text)
    if status_match:
        exit_code = int(status_match.group(1))

    return {
        "status_files": status_files,
        "stderr_files": stderr_files,
        "stdout_files": stdout_files,
        "exit_code": exit_code,
        "status_text": status_text,
        "stderr_text": stderr_text,
        "stdout_text": stdout_text,
    }


def _format_wrap_launcher_artifacts(artifacts):
    if not artifacts:
        return ""

    parts = []
    if artifacts.get("exit_code") is not None:
        parts.append("strace_exit=%s" % artifacts["exit_code"])

    stderr_text = (artifacts.get("stderr_text") or "").strip()
    if stderr_text:
        parts.append("stderr=%s" % _compact_text(stderr_text, max_len=260))

    stdout_text = (artifacts.get("stdout_text") or "").strip()
    if stdout_text:
        parts.append("stdout=%s" % _compact_text(stdout_text, max_len=180))

    return "; ".join(parts)


def _wrap_launch_failed(diagnostics, launcher_artifacts, package_name):
    if launcher_artifacts and launcher_artifacts.get("exit_code") not in [None, 0]:
        return True

    stderr_text = (launcher_artifacts.get("stderr_text") or "").lower()
    fatal_markers = [
        "invalid system call",
        "unexpected wait status",
        "permission denied",
        "no such file or directory",
        "not executable",
        "usage: strace",
    ]
    if any(marker in stderr_text for marker in fatal_markers):
        return True

    return not (diagnostics.get("pid_values") or [])


def _relaunch_activity_without_wrap(configs, device, package_name, activity_name):
    launch_command = _build_wrap_launch_command(configs, activity_name)
    _run_device_shell(
        configs,
        device,
        "setprop wrap.%s ''" % package_name,
        require_root=True,
    )
    _run_device_shell(configs, device, "am force-stop %s" % shlex.quote(package_name), require_root=False)
    return _run_device_shell(
        configs,
        device,
        launch_command,
        require_root=False,
    )


def _write_temp_script(contents):
    temp_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8")
    try:
        temp_file.write(contents)
        temp_file.flush()
    finally:
        temp_file.close()
    return temp_file.name


def _push_host_file(configs, device, host_path, remote_path):
    return _run_host_cmd(
        _adb_base(configs, device) + ["push", host_path, remote_path],
        timeout_sec=max(_trace_cmd_timeout(configs), 30),
    )


def _device_command_exists(configs, device, command_name, require_root=False):
    command_name = str(command_name or "").strip()
    if not command_name:
        return False
    if "/" in command_name:
        probe_command = "[ -x %s ]" % shlex.quote(command_name)
    else:
        probe_command = "command -v %s >/dev/null 2>&1" % shlex.quote(command_name)
    result = _run_device_shell(
        configs,
        device,
        probe_command,
        require_root=require_root,
    )
    return result is not None and result.returncode == 0


def _push_wrap_script(configs, device, package_name, device_trace_tool, wrapper_device_path, trace_dir):
    wrapper_script = _build_wrap_script(configs, package_name, device_trace_tool, trace_dir)
    local_wrapper_path = _write_temp_script(wrapper_script)
    try:
        wrapper_push_result = _push_host_file(configs, device, local_wrapper_path, wrapper_device_path)
    finally:
        try:
            os.unlink(local_wrapper_path)
        except OSError:
            pass
    if wrapper_push_result.returncode != 0:
        return wrapper_push_result
    _run_device_shell(configs, device, "chmod 755 %s" % shlex.quote(wrapper_device_path), require_root=True)
    return wrapper_push_result


def _build_wrap_script(configs, package_name, device_trace_tool, trace_dir):
    trace_filter = configs.get(
        "DEVICE_TRACE_FILTER",
        "fork,vfork,clone,clone3,execve,execveat,kill,exit,exit_group,open,openat,openat2,unlink,unlinkat,rename,renameat,renameat2,connect,listen,accept,accept4",
    )
    trace_string_size = int(configs.get("DEVICE_TRACE_STRING_SIZE", 256))
    return "\n".join(
        [
            "#!/system/bin/sh",
            f"TRACE_DIR={trace_dir}",
            "TRACE_ID=$(date +%s).$$",
            'TRACE_FILE="$TRACE_DIR/trace.$TRACE_ID"',
            'TRACE_STDERR="$TRACE_DIR/trace_launcher.err.$TRACE_ID"',
            'TRACE_STDOUT="$TRACE_DIR/trace_stdout.log.$TRACE_ID"',
            'TRACE_STATUS="$TRACE_DIR/trace_status.log.$TRACE_ID"',
            "",
            'mkdir -p "$TRACE_DIR"',
            "",
            "{",
            '  echo "===== wrap start ====="',
            '  echo "trace_id=$TRACE_ID"',
            "  i=0",
            '  for a in "$@"; do',
            '    echo "arg$i=$a"',
            "    i=$((i+1))",
            "  done",
            '} >> "$TRACE_DIR/wrap.log"',
            "",
            f'{device_trace_tool} -f -tt -s {trace_string_size} -e trace={trace_filter} -o "$TRACE_FILE" -- "$@" >>"$TRACE_STDOUT" 2>>"$TRACE_STDERR"',
            "",
            "status=$?",
            'echo "strace_exit=$status" >> "$TRACE_STATUS"',
            "exit $status",
            "",
        ]
    )


def _candidate_wrap_device_tools(configs, pushed_device_trace_tool):
    candidates = []
    for raw_value in [
        pushed_device_trace_tool,
        str(configs.get("DEVICE_TRACE_TOOL", "strace") or "strace").strip(),
        "strace",
        "/system/bin/strace",
        "/system/xbin/strace",
    ]:
        value = str(raw_value or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _prepare_wrap_runtime_for_tool(
    configs,
    device,
    package_name,
    activity_name,
    runtime_key,
    device_trace_tool,
    wrapper_device_path,
    trace_dir,
):
    property_value = f"/system/bin/sh {wrapper_device_path}"
    setprop_result = _run_device_shell(
        configs,
        device,
        "setprop wrap.%s %s" % (package_name, shlex.quote(property_value)),
        require_root=True,
    )
    if setprop_result is None or setprop_result.returncode != 0:
        return None, "Failed to set wrap.%s on the device." % package_name

    _run_device_shell(configs, device, "am force-stop %s" % shlex.quote(package_name), require_root=False)
    launch_command = _build_wrap_launch_command(configs, activity_name)
    launch_result = _run_device_shell(
        configs,
        device,
        launch_command,
        require_root=False,
    )
    launch_output = "%s\n%s" % (
        "" if launch_result is None else (launch_result.stdout or ""),
        "" if launch_result is None else (launch_result.stderr or ""),
    )
    if (
        launch_result is None
        or launch_result.returncode != 0
        or "Error type" in launch_output
        or "does not exist" in launch_output
    ):
        _run_device_shell(
            configs,
            device,
            "setprop wrap.%s ''" % package_name,
            require_root=True,
        )
        return None, "Failed to relaunch %s under wrap-mode guest-side strace. launch command: %s. am start output: %s" % (
            activity_name,
            _compact_text(launch_command, max_len=220),
            launch_output.strip(),
        )

    launch_wait = float(configs.get("DEVICE_TRACE_WRAP_LAUNCH_WAIT", 3.0))
    if launch_wait > 0:
        time.sleep(launch_wait)

    diagnostics = _collect_launch_diagnostics(configs, device, package_name)
    launcher_artifacts = _collect_wrap_launcher_artifacts(configs, device, package_name, trace_dir)
    if _wrap_launch_failed(diagnostics, launcher_artifacts, package_name):
        _run_device_shell(
            configs,
            device,
            "setprop wrap.%s ''" % package_name,
            require_root=True,
        )
        failure_lines = [
            "Wrap-mode launch for %s did not stay alive using trace tool %s." % (activity_name, device_trace_tool),
            "Launch command: %s" % _compact_text(launch_command, max_len=260),
            "Launch diagnostics: %s" % _format_launch_diagnostics(diagnostics),
        ]
        launcher_summary = _format_wrap_launcher_artifacts(launcher_artifacts)
        if launcher_summary:
            failure_lines.append("Trace launcher diagnostics: %s" % launcher_summary)
        return None, "\n".join(failure_lines)

    runtime = {
        "key": runtime_key,
        "ready": True,
        "package": package_name,
        "activity": activity_name,
        "launch_command": launch_command,
        "property_value": property_value,
        "device_trace_tool": device_trace_tool,
        "wrapper_device_path": wrapper_device_path,
        "remote_trace_dir": trace_dir,
        "remote_trace_prefix": trace_dir.rstrip("/") + "/trace.",
        "remote_wrap_log": trace_dir.rstrip("/") + "/wrap.log",
        "start_failures": [],
        "prepared_at": int(time.time()),
    }
    if _launch_looks_suspicious(diagnostics, package_name):
        print_with_color(
            "Wrap launch for %s looks incomplete. %s"
            % (package_name, _format_launch_diagnostics(diagnostics)),
            "yellow",
        )
    return runtime, ""


def _prepare_wrap_runtime(configs, device, packages):
    package_name = _resolve_wrap_package(configs, packages)
    activity_name = _resolve_wrap_activity(configs, device=device, package_name=package_name)
    runtime_key = (configs.get("ADB_PATH", "adb"), device, package_name)
    existing = _wrap_runtime_state.get(runtime_key)
    if existing:
        return existing

    if not package_name:
        return _wrap_runtime_failure(
            configs,
            device,
            package_name,
            activity_name,
            "DEVICE_TRACE_MODE=wrap requires DEVICE_TRACE_WRAP_PACKAGE, GUARD_ALLOWED_PACKAGE, or a detectable package.",
        )

    if not activity_name:
        return _wrap_runtime_failure(
            configs,
            device,
            package_name,
            activity_name,
            "DEVICE_TRACE_MODE=wrap requires DEVICE_TRACE_WRAP_ACTIVITY to relaunch the app under guest-side strace.",
        )

    device_trace_tool = str(
        configs.get("DEVICE_TRACE_WRAP_STRACE_DEVICE", "/data/local/tmp/strace_new") or "/data/local/tmp/strace_new"
    ).strip()
    wrapper_device_path = str(
        configs.get("DEVICE_TRACE_WRAP_SCRIPT_DEVICE", "/data/local/tmp/appagent_wrap_trace.sh")
        or "/data/local/tmp/appagent_wrap_trace.sh"
    ).strip()

    if not _can_run_as_package(configs, device, package_name):
        return _wrap_runtime_failure(
            configs,
            device,
            package_name,
            activity_name,
            f"DEVICE_TRACE_MODE=wrap requires a debuggable app with run-as access, but run-as is unavailable for {package_name}.",
        )

    if not _ensure_adb_root(configs, device):
        return _wrap_runtime_failure(
            configs,
            device,
            package_name,
            activity_name,
            "DEVICE_TRACE_MODE=wrap requires adb root on this emulator/device, but adb root is unavailable.",
        )

    _run_device_shell(configs, device, "setenforce 0", require_root=True)

    paths = _wrap_trace_paths(package_name)

    prepare_result = _run_device_shell_run_as(
        configs,
        device,
        package_name,
        "mkdir -p files/appagent_guard && rm -f files/appagent_guard/*",
    )
    if prepare_result is None or prepare_result.returncode != 0:
        return _wrap_runtime_failure(
            configs,
            device,
            package_name,
            activity_name,
            "Failed to prepare the app-private trace directory via run-as for %s." % package_name,
        )

    host_trace_tool = str(configs.get("DEVICE_TRACE_WRAP_STRACE_HOST", "") or "").strip()
    host_trace_tool_valid = bool(host_trace_tool and os.path.isfile(host_trace_tool))
    tool_failures = []
    for candidate_tool in _candidate_wrap_device_tools(configs, device_trace_tool):
        candidate_exists = _device_command_exists(configs, device, candidate_tool, require_root=False)
        using_pushed_host_binary = bool(
            host_trace_tool_valid
            and candidate_tool == device_trace_tool
            and candidate_tool.startswith("/")
            and not candidate_exists
        )
        if using_pushed_host_binary:
            push_result = _push_host_file(configs, device, host_trace_tool, device_trace_tool)
            if push_result.returncode != 0:
                tool_failures.append(
                    "Failed to push DEVICE_TRACE_WRAP_STRACE_HOST to %s: %s"
                    % (candidate_tool, push_result.stderr or push_result.stdout)
                )
                continue
            _run_device_shell(configs, device, "chmod 755 %s" % shlex.quote(device_trace_tool), require_root=True)
        elif not candidate_exists:
            if candidate_tool == device_trace_tool and not host_trace_tool_valid and candidate_tool.startswith("/"):
                tool_failures.append(
                    "Skipped pushed wrap trace tool %s because DEVICE_TRACE_WRAP_STRACE_HOST is missing or invalid."
                    % candidate_tool
                )
                continue
            tool_failures.append("Device trace tool %s is unavailable on the device." % candidate_tool)
            continue

        wrapper_push_result = _push_wrap_script(
            configs,
            device,
            package_name,
            candidate_tool,
            wrapper_device_path,
            paths["trace_dir"],
        )
        if wrapper_push_result.returncode != 0:
            tool_failures.append(
                "Failed to push wrap trace launcher script for %s: %s"
                % (candidate_tool, wrapper_push_result.stderr or wrapper_push_result.stdout)
            )
            continue

        runtime, runtime_error = _prepare_wrap_runtime_for_tool(
            configs=configs,
            device=device,
            package_name=package_name,
            activity_name=activity_name,
            runtime_key=runtime_key,
            device_trace_tool=candidate_tool,
            wrapper_device_path=wrapper_device_path,
            trace_dir=paths["trace_dir"],
        )
        if runtime:
            _wrap_runtime_state[runtime_key] = runtime
            print_with_color(
                "Prepared wrap-mode guest-side syscall trace runtime for %s (%s) via %s."
                % (package_name, activity_name, candidate_tool),
                "cyan",
            )
            return runtime
        tool_failures.append(runtime_error)

    relaunch_result = _relaunch_activity_without_wrap(configs, device, package_name, activity_name)
    relaunch_output = "%s\n%s" % (
        "" if relaunch_result is None else (relaunch_result.stdout or ""),
        "" if relaunch_result is None else (relaunch_result.stderr or ""),
    )
    fallback_wait = float(configs.get("DEVICE_TRACE_WRAP_LAUNCH_WAIT", 3.0))
    if fallback_wait > 0:
        time.sleep(fallback_wait)
    failure_lines = [
        "Wrap-mode launch for %s did not stay alive; disabled wrap tracing for this run." % activity_name,
    ]
    failure_lines.extend([message for message in tool_failures if message])
    if relaunch_output.strip():
        failure_lines.append("Fallback relaunch output: %s" % _compact_text(relaunch_output.strip(), max_len=320))
    return _wrap_runtime_failure(
        configs,
        device,
        package_name,
        activity_name,
        "\n".join(failure_lines),
    )


def prepare_device_trace_runtime(configs, device, packages=None):
    if not configs.get("DEVICE_TRACE_ENABLED", False):
        return None
    if _trace_mode(configs) != "wrap":
        return None
    return _prepare_wrap_runtime(configs, device, packages or [])


def cleanup_device_trace_runtime(configs, device):
    if _trace_mode(configs) != "wrap":
        return None

    adb_path = configs.get("ADB_PATH", "adb")
    cleanup_keys = []
    for key, runtime in list(_wrap_runtime_state.items()):
        if key[0] != adb_path or key[1] != device:
            continue
        package_name = runtime.get("package", "")
        if package_name:
            _run_device_shell(
                configs,
                device,
                "setprop wrap.%s ''" % package_name,
                require_root=True,
            )
            if configs.get("DEVICE_TRACE_WRAP_FORCE_STOP_ON_CLEANUP", False):
                _run_device_shell(
                    configs,
                    device,
                    "am force-stop %s" % shlex.quote(package_name),
                    require_root=False,
                )
            print_with_color(
                "Cleared wrap-mode guest-side syscall trace runtime for %s." % package_name,
                "cyan",
            )
        cleanup_keys.append(key)

    for key in cleanup_keys:
        _wrap_runtime_state.pop(key, None)

    return True


def _list_remote_wrap_trace_files_run_as(configs, device, package_name, remote_trace_prefix):
    return _list_remote_trace_files_run_as(configs, device, package_name, remote_trace_prefix)


def _remote_file_size_run_as(configs, device, package_name, remote_file):
    command = "if [ -f {path} ]; then wc -c < {path}; else echo 0; fi".format(
        path=shlex.quote(remote_file)
    )
    result = _run_device_shell_run_as(configs, device, package_name, command)
    if result is None or result.returncode != 0:
        return 0
    text = (result.stdout or "").strip()
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


def _extract_file_delta(local_snapshot_path, start_offset, local_output_path):
    with open(local_snapshot_path, "rb") as infile:
        payload = infile.read()

    if start_offset < 0 or start_offset > len(payload):
        start_offset = 0

    delta = payload[start_offset:]
    if not delta:
        return False

    with open(local_output_path, "wb") as outfile:
        outfile.write(delta)
    return True


def _start_device_trace_wrap(configs, device, trace_dir, trace_name, packages):
    os.makedirs(trace_dir, exist_ok=True)
    metadata = _trace_metadata(configs, mode="wrap")
    runtime = _prepare_wrap_runtime(configs, device, packages)

    package_name = _resolve_wrap_package(configs, packages)
    packages_seen = _unique_keep_order(([package_name] if package_name else []) + list(packages or []))
    base_payload = {
        **metadata,
        "packages": packages_seen,
        "missing_packages": [],
        "start_failures": [],
        "targets": [],
        "local_trace_dir": os.path.abspath(trace_dir),
        "remote_trace_dir": "",
        "captured_at": int(time.time()),
        "trace_name": trace_name,
        "trace_mode": "wrap",
    }

    if not runtime or not runtime.get("ready"):
        failures = []
        if runtime:
            failures = runtime.get("start_failures", [])
        elif package_name:
            failures = [{"package": package_name, "stderr": "Unknown wrap runtime preparation failure."}]
        print_with_color(
            "Wrap-mode guest-side syscall trace runtime is unavailable for %s." % (package_name or "(unknown package)"),
            "red",
        )
        return {
            **base_payload,
            "start_failures": failures,
        }

    remote_trace_files = _list_remote_wrap_trace_files_run_as(
        configs,
        device,
        runtime["package"],
        runtime["remote_trace_prefix"],
    )
    baseline_offsets = {}
    for remote_file in remote_trace_files:
        baseline_offsets[remote_file] = _remote_file_size_run_as(
            configs,
            device,
            runtime["package"],
            remote_file,
        )

    return {
        **base_payload,
        "wrap_runtime_key": runtime["key"],
        "baseline_offsets": baseline_offsets,
        "remote_trace_dir": runtime["remote_trace_dir"],
        "targets": [
            {
                "package": runtime["package"],
                "activity": runtime["activity"],
                "trace_launch_mode": "wrap",
                "remote_trace_files": remote_trace_files,
                "remote_wrap_log": runtime["remote_wrap_log"],
                "baseline_offsets": baseline_offsets,
            }
        ],
    }


def _stop_device_trace_wrap(configs, device, session):
    if not session:
        return None

    stop_delay = float(configs.get("DEVICE_TRACE_STOP_DELAY", 1.0))
    if stop_delay > 0:
        time.sleep(stop_delay)

    runtime = _wrap_runtime_state.get(session.get("wrap_runtime_key"))
    metadata = _trace_metadata(configs, mode="wrap")
    if not runtime or not runtime.get("ready"):
        trace_info = {
            **metadata,
            "packages": session.get("packages", []),
            "missing_packages": session.get("missing_packages", []),
            "start_failures": session.get("start_failures", []),
            "targets": session.get("targets", []),
            "trace_files": [],
            "trace_file_count": 0,
            "local_trace_dir": session.get("local_trace_dir", ""),
            "remote_trace_dir": session.get("remote_trace_dir", ""),
            "captured_at": int(time.time()),
        }
        print_with_color(
            "Wrap-mode guest-side syscall trace stopped, but the persistent runtime was unavailable.",
            "red",
        )
        return trace_info

    local_trace_dir = session.get("local_trace_dir", "")
    os.makedirs(local_trace_dir, exist_ok=True)

    remote_trace_files = _list_remote_wrap_trace_files_run_as(
        configs,
        device,
        runtime["package"],
        runtime["remote_trace_prefix"],
    )
    baseline_offsets = session.get("baseline_offsets", {})
    trace_files = []
    target_info = {
        "package": runtime["package"],
        "activity": runtime["activity"],
        "trace_launch_mode": "wrap",
        "remote_trace_files": remote_trace_files,
        "remote_wrap_log": runtime["remote_wrap_log"],
        "baseline_offsets": baseline_offsets,
    }

    temp_pull_dir = os.path.join(local_trace_dir, ".wrap_snapshots")
    os.makedirs(temp_pull_dir, exist_ok=True)

    for remote_file in remote_trace_files:
        start_offset = baseline_offsets.get(remote_file, 0)
        current_size = _remote_file_size_run_as(configs, device, runtime["package"], remote_file)
        if current_size < start_offset:
            start_offset = 0
        if current_size <= start_offset:
            continue

        local_snapshot_path, pull_result = _pull_run_as_file(
            configs,
            device,
            runtime["package"],
            remote_file,
            temp_pull_dir,
        )
        if pull_result.returncode != 0:
            target_info.setdefault("pull_errors", []).append(
                {
                    "remote_file": remote_file,
                    "returncode": pull_result.returncode,
                    "stderr": pull_result.stderr,
                }
            )
            continue

        local_delta_path = os.path.abspath(
            os.path.join(
                local_trace_dir,
                "%s__%s" % (_sanitize_name(session.get("trace_name", "trace")), os.path.basename(remote_file)),
            )
        )
        if _extract_file_delta(local_snapshot_path, start_offset, local_delta_path):
            trace_files.append(local_delta_path)
        try:
            os.remove(local_snapshot_path)
        except OSError:
            pass

    trace_info = {
        **metadata,
        "packages": session.get("packages", []),
        "missing_packages": session.get("missing_packages", []),
        "start_failures": session.get("start_failures", []),
        "targets": [target_info],
        "trace_files": sorted(trace_files),
        "trace_file_count": len(trace_files),
        "local_trace_dir": local_trace_dir,
        "remote_trace_dir": runtime["remote_trace_dir"],
        "captured_at": int(time.time()),
    }
    if not trace_files:
        print_with_color(
            "Wrap-mode guest-side syscall trace snapshot produced no new syscall bytes for this step.",
            "red",
        )
    return trace_info


def _start_device_trace_attach(configs, device, trace_dir, trace_name, packages):
    os.makedirs(trace_dir, exist_ok=True)

    metadata = _trace_metadata(configs, mode="attach")
    trace_tool = metadata["trace_tool"]
    trace_filter = metadata["trace_filter"]
    trace_filter_candidates = metadata.get("trace_filter_candidates", [trace_filter])
    trace_string_size = int(configs.get("DEVICE_TRACE_STRING_SIZE", 256))
    remote_dir = configs.get("DEVICE_TRACE_REMOTE_DIR", "/data/local/tmp/appagent_guard")

    targets = []
    missing_packages = []
    start_failures = []
    for package_name in _unique_keep_order(packages):
        pids = _query_package_pids(configs, device, package_name)
        if not pids:
            missing_packages.append(package_name)
            continue

        for pid in pids:
            use_run_as = _can_run_as_package(configs, device, package_name)
            package_remote_dir = _run_as_trace_dir(package_name) if use_run_as else remote_dir
            remote_prefix = "%s/%s_%s" % (
                remote_dir.rstrip("/"),
                _sanitize_name(trace_name),
                _sanitize_name("%s_%s" % (package_name, pid)),
            )
            if use_run_as:
                remote_prefix = "%s/%s_%s" % (
                    package_remote_dir.rstrip("/"),
                    _sanitize_name(trace_name),
                    _sanitize_name("%s_%s" % (package_name, pid)),
                )
            remote_glob_expr = _remote_glob_expr(remote_prefix)
            sidecar_paths = _trace_sidecar_paths(remote_prefix)
            target_started = False
            for filter_candidate in trace_filter_candidates:
                trace_command = (
                    "mkdir -p {remote_dir} && "
                    "rm -f {remote_glob_expr} {trace_stderr} && "
                    "{trace_tool} -ff -tt -s {trace_string_size} -e trace={trace_filter} "
                    "-p {pid} -o {remote_prefix} >/dev/null 2>{trace_stderr} & "
                    "tracer_pid=$! && echo $tracer_pid && sleep 0.2 && kill -0 $tracer_pid >/dev/null 2>&1"
                ).format(
                    remote_dir=shlex.quote(package_remote_dir if use_run_as else remote_dir),
                    remote_glob_expr=remote_glob_expr,
                    trace_stderr=shlex.quote(sidecar_paths["stderr"]),
                    trace_tool=shlex.quote(trace_tool),
                    trace_string_size=trace_string_size,
                    trace_filter=filter_candidate,
                    pid=pid,
                    remote_prefix=shlex.quote(remote_prefix),
                )
                if use_run_as:
                    result = _run_device_shell_run_as(configs, device, package_name, trace_command)
                else:
                    result = _run_device_shell(configs, device, trace_command, require_root=True)
                tracer_pid = ""
                if result is not None and result.stdout.strip():
                    tracer_pid = result.stdout.strip().splitlines()[0].strip()

                if result is not None and result.returncode == 0 and tracer_pid.isdigit():
                    targets.append(
                        {
                            "package": package_name,
                            "pid": pid,
                            "remote_prefix": remote_prefix,
                            "remote_glob_expr": remote_glob_expr,
                            "remote_stderr_path": sidecar_paths["stderr"],
                            "tracer_pid": tracer_pid,
                            "trace_command": trace_command,
                            "trace_filter_selected": filter_candidate,
                            "trace_launch_mode": "run_as" if use_run_as else "root",
                            "run_as_package": package_name if use_run_as else "",
                        }
                    )
                    target_started = True
                    break

                if use_run_as:
                    stderr_result = _run_device_shell_run_as(
                        configs,
                        device,
                        package_name,
                        "cat %s 2>/dev/null" % shlex.quote(sidecar_paths["stderr"]),
                    )
                    stderr_text = ""
                    if stderr_result is not None and stderr_result.returncode == 0:
                        stderr_text = (stderr_result.stdout or "").strip()
                else:
                    stderr_text = _read_remote_text(configs, device, sidecar_paths["stderr"], require_root=True)
                start_failures.append(
                    {
                        "package": package_name,
                        "pid": pid,
                        "trace_filter_attempted": filter_candidate,
                        "trace_command": trace_command,
                        "returncode": None if result is None else result.returncode,
                        "stdout": "" if result is None else result.stdout,
                        "stderr": stderr_text or ("" if result is None else result.stderr),
                    }
                )

            if not target_started:
                print_with_color(
                    "Device-side syscall trace could not attach to %s (pid=%s)." % (package_name, pid),
                    "red",
                )

    if not targets:
        failure_summary = ""
        if start_failures:
            first_failure = start_failures[0]
            stderr_text = (first_failure.get("stderr") or "").strip()
            if stderr_text:
                failure_summary = " First launcher error: %s" % stderr_text.splitlines()[-1]
            summary_message = "Device-side syscall trace could not attach to any target PID.%s" % failure_summary
        else:
            summary_message = (
                "Device-side syscall trace could not start because no target package PID was found: %s."
                % (", ".join(missing_packages) if missing_packages else "(none)")
            )
        print_with_color(summary_message, "red")
        return {
            **metadata,
            "trace_files": [],
            "targets": [],
            "packages": _unique_keep_order(packages),
            "missing_packages": missing_packages,
            "start_failures": start_failures,
            "local_trace_dir": os.path.abspath(trace_dir),
            "remote_trace_dir": remote_dir,
            "captured_at": int(time.time()),
            "trace_name": trace_name,
        }

    return {
        **metadata,
        "packages": _unique_keep_order(packages),
        "missing_packages": missing_packages,
        "start_failures": start_failures,
        "targets": targets,
        "local_trace_dir": os.path.abspath(trace_dir),
        "remote_trace_dir": remote_dir,
        "captured_at": int(time.time()),
        "trace_name": trace_name,
    }


def _stop_device_trace_attach(configs, device, session):
    if not session:
        return None

    targets = session.get("targets", [])
    if not targets:
        return session

    stop_delay = float(configs.get("DEVICE_TRACE_STOP_DELAY", 1.0))
    if stop_delay > 0:
        time.sleep(stop_delay)

    for target in targets:
        tracer_pid = target.get("tracer_pid", "")
        if tracer_pid:
            if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                _run_device_shell_run_as(
                    configs,
                    device,
                    target["run_as_package"],
                    "kill -INT %s" % tracer_pid,
                )
            else:
                _run_device_shell(configs, device, "kill -INT %s" % tracer_pid, require_root=True)

    trace_files = []
    for target in targets:
        remote_prefix = target["remote_prefix"]
        if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
            remote_files = _list_remote_trace_files_run_as(
                configs,
                device,
                target["run_as_package"],
                remote_prefix,
            )
        else:
            remote_files = _list_remote_trace_files(configs, device, remote_prefix)
        target["remote_trace_files"] = remote_files
        for remote_file in remote_files:
            if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                local_file, pull_result = _pull_run_as_file(
                    configs,
                    device,
                    target["run_as_package"],
                    remote_file,
                    session["local_trace_dir"],
                )
            else:
                local_file = os.path.abspath(os.path.join(session["local_trace_dir"], os.path.basename(remote_file)))
                pull_result = _run_host_cmd(
                    _adb_base(configs, device) + ["pull", remote_file, session["local_trace_dir"]],
                    timeout_sec=_trace_cmd_timeout(configs),
                )
            if pull_result.returncode == 0:
                if os.path.isfile(local_file):
                    trace_files.append(local_file)
            else:
                target.setdefault("pull_errors", []).append(
                    {
                        "remote_file": remote_file,
                        "returncode": pull_result.returncode,
                        "stderr": pull_result.stderr,
                    }
                )
        if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
            _run_device_shell_run_as(
                configs,
                device,
                target["run_as_package"],
                "rm -f %s" % target.get("remote_glob_expr", _remote_glob_expr(remote_prefix)),
            )
        else:
            _run_device_shell(
                configs,
                device,
                "rm -f %s" % target.get("remote_glob_expr", _remote_glob_expr(remote_prefix)),
                require_root=True,
            )
        if target.get("remote_stderr_path"):
            if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                _run_device_shell_run_as(
                    configs,
                    device,
                    target["run_as_package"],
                    "rm -f %s" % shlex.quote(target["remote_stderr_path"]),
                )
            else:
                _run_device_shell(
                    configs,
                    device,
                    "rm -f %s" % shlex.quote(target["remote_stderr_path"]),
                    require_root=True,
                )

    trace_info = {
        "trace_scope": session.get("trace_scope", "android_guest_syscalls_via_strace"),
        "trace_capture_method": session.get("trace_capture_method", "guest-side strace attach (-p PID)"),
        "trace_semantics": session.get(
            "trace_semantics",
            "Syscalls issued by the traced Android app process inside the emulator guest OS.",
        ),
        "requested_trace_mode": session.get("requested_trace_mode", session.get("trace_mode", "attach")),
        "trace_mode": session.get("trace_mode", "attach"),
        "trace_tool": session.get("trace_tool", configs.get("DEVICE_TRACE_TOOL", "strace")),
        "trace_filter": session.get("trace_filter", configs.get("DEVICE_TRACE_FILTER", "")),
        "trace_filter_candidates": session.get("trace_filter_candidates", []),
        "packages": session.get("packages", []),
        "missing_packages": session.get("missing_packages", []),
        "start_failures": session.get("start_failures", []),
        "wrap_start_failures": session.get("wrap_start_failures", []),
        "targets": targets,
        "trace_files": sorted(trace_files),
        "trace_file_count": len(trace_files),
        "local_trace_dir": session.get("local_trace_dir", ""),
        "remote_trace_dir": session.get("remote_trace_dir", ""),
        "captured_at": int(time.time()),
    }
    if not trace_files:
        if session.get("start_failures"):
            first_failure = session["start_failures"][0]
            failure_text = (first_failure.get("stderr") or "").strip()
            if failure_text:
                print_with_color(
                    "Trace launcher stderr: %s" % failure_text.splitlines()[-1],
                    "red",
                )
        print_with_color(
            "Device-side syscall trace stopped, but no guest-side strace output files were collected.",
            "red",
        )
    return trace_info


def start_device_trace(configs, device, trace_dir, trace_name, packages):
    if not configs.get("DEVICE_TRACE_ENABLED", False):
        return None

    if _trace_mode(configs) == "wrap":
        return _start_device_trace_wrap(configs, device, trace_dir, trace_name, packages)
    return _start_device_trace_attach(configs, device, trace_dir, trace_name, packages)


def stop_device_trace(configs, device, session):
    if not session:
        return None

    if session.get("trace_mode") == "wrap" or session.get("wrap_runtime_key"):
        return _stop_device_trace_wrap(configs, device, session)
    return _stop_device_trace_attach(configs, device, session)

