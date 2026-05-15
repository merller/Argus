import os
import re
import shlex
import subprocess
import tempfile
import time


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ["1", "true", "yes", "y", "on"]


def _get_first_env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


class GuardTraceManager(object):
    def __init__(self, adb):
        self.adb = adb
        self.trace_context = None
        self.last_trace_info = None
        self._adb_root_ready = None
        self._run_as_cache = {}
        self._wrap_runtime = None

    @staticmethod
    def _sanitize_name(value):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "unknown")

    @staticmethod
    def _unique_keep_order(values):
        seen = set()
        ordered = []
        for value in values:
            if value and value not in seen:
                ordered.append(value)
                seen.add(value)
        return ordered

    @staticmethod
    def _as_text(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _adb_base(self):
        return list(self.adb.cmd_prefix)

    def _trace_cmd_timeout(self):
        try:
            return float(_get_first_env("AUTODROID_GUARD_TRACE_CMD_TIMEOUT", "DEVICE_TRACE_CMD_TIMEOUT", "GUARD_TRACE_CMD_TIMEOUT", default="12"))
        except Exception:
            return 12.0

    def _trace_mode(self):
        mode = (_get_first_env("AUTODROID_GUARD_TRACE_MODE", "DEVICE_TRACE_MODE", "GUARD_TRACE_MODE", default="attach") or "attach").strip().lower()
        if mode not in ["attach", "wrap"]:
            return "attach"
        return mode

    def _manual_app_launch_mode(self):
        return _env_flag("AUTODROID_MANUAL_APP_LAUNCH", False)

    def _trace_filter(self):
        return _get_first_env(
            "AUTODROID_GUARD_TRACE_FILTER",
            "DEVICE_TRACE_FILTER",
            "GUARD_TRACE_FILTER",
            default="fork,vfork,clone,clone3,execve,execveat,kill,exit,exit_group,open,openat,openat2,unlink,unlinkat,rename,renameat,renameat2,connect,listen,accept,accept4",
        )

    def _trace_metadata(self, mode=None):
        resolved_mode = mode or self._trace_mode()
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
            "trace_tool": _get_first_env("AUTODROID_GUARD_TRACE_TOOL", "DEVICE_TRACE_TOOL", "TRACE_TOOL", default="strace"),
            "trace_filter": self._trace_filter(),
            "trace_filter_candidates": self._build_trace_filter_candidates(self._trace_filter()),
        }

    def _build_trace_filter_candidates(self, trace_filter):
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
        return self._unique_keep_order(candidates)

    def _run_host_cmd(self, cmd, timeout_sec=None):
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
                stdout=self._as_text(result.stdout),
                stderr=self._as_text(result.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                cmd,
                124,
                stdout=self._as_text(exc.stdout),
                stderr=self._as_text(exc.stderr) + "\nTIMEOUT after %ss" % timeout_sec,
            )

    def _ensure_adb_root(self):
        if self._adb_root_ready is not None:
            return self._adb_root_ready
        if not _env_flag("AUTODROID_GUARD_TRACE_USE_ADB_ROOT", True):
            self._adb_root_ready = False
            return False

        timeout_sec = self._trace_cmd_timeout()
        root_result = self._run_host_cmd(self._adb_base() + ["root"], timeout_sec=timeout_sec)
        combined_output = "%s\n%s" % (root_result.stdout or "", root_result.stderr or "")
        normalized = combined_output.lower()
        root_requested = (
            root_result.returncode == 0
            and (
                "restarting adbd as root" in normalized
                or "adbd is already running as root" in normalized
            )
        )
        if not root_requested:
            self._adb_root_ready = False
            return False

        self._run_host_cmd(self._adb_base() + ["wait-for-device"], timeout_sec=max(timeout_sec, 20))
        time.sleep(1.0)
        id_result = self._run_host_cmd(self._adb_base() + ["shell", "id"], timeout_sec=timeout_sec)
        self._adb_root_ready = id_result.returncode == 0 and "uid=0" in (id_result.stdout or "")
        return self._adb_root_ready

    def _can_run_as_package(self, package_name):
        if package_name in self._run_as_cache:
            return self._run_as_cache[package_name]
        if not package_name or not _env_flag("AUTODROID_GUARD_TRACE_USE_RUN_AS", True):
            self._run_as_cache[package_name] = False
            return False
        timeout_sec = self._trace_cmd_timeout()
        result = self._run_host_cmd(
            self._adb_base() + ["shell", "run-as %s id" % shlex.quote(package_name)],
            timeout_sec=timeout_sec,
        )
        available = result.returncode == 0 and "uid=" in (result.stdout or "")
        self._run_as_cache[package_name] = available
        return available

    def _run_device_shell(self, shell_command, require_root=False, fallback_to_non_root=True):
        quoted = shlex.quote(str(shell_command or ""))
        variants = []
        if require_root and self._ensure_adb_root():
            variants.append(self._adb_base() + ["shell", "sh -c %s" % quoted])
        if require_root and _env_flag("AUTODROID_GUARD_TRACE_USE_SU", True):
            variants.append(self._adb_base() + ["shell", "su 0 sh -c %s" % quoted])
        if not require_root or fallback_to_non_root or not variants:
            variants.append(self._adb_base() + ["shell", "sh -c %s" % quoted])

        first_result = None
        last_result = None
        timeout_sec = self._trace_cmd_timeout()
        for cmd in variants:
            result = self._run_host_cmd(cmd, timeout_sec=timeout_sec)
            if first_result is None:
                first_result = result
            last_result = result
            if result.returncode == 0:
                return result
        return first_result or last_result

    def _run_device_shell_run_as(self, package_name, shell_command):
        timeout_sec = self._trace_cmd_timeout()
        quoted = shlex.quote(str(shell_command or ""))
        cmd = self._adb_base() + [
            "shell",
            "run-as %s sh -c %s" % (shlex.quote(package_name), quoted),
        ]
        return self._run_host_cmd(cmd, timeout_sec=timeout_sec)

    def _remote_glob_expr(self, remote_prefix):
        normalized = (remote_prefix or "").rstrip("/")
        remote_dir = os.path.dirname(normalized) or "."
        remote_name = os.path.basename(normalized)
        return "%s/%s*" % (shlex.quote(remote_dir), shlex.quote(remote_name))

    def _list_remote_trace_files(self, remote_prefix):
        glob_expr = self._remote_glob_expr(remote_prefix)
        command = (
            "for path in {glob_expr}; do "
            "if [ -f \"$path\" ]; then printf '%s\\n' \"$path\"; fi; "
            "done"
        ).format(glob_expr=glob_expr)
        result = self._run_device_shell(command, require_root=True)
        if result is None or result.returncode != 0:
            return []
        return sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])

    def _list_remote_trace_files_run_as(self, package_name, remote_prefix):
        glob_expr = self._remote_glob_expr(remote_prefix)
        command = (
            "for path in {glob_expr}; do "
            "if [ -f \"$path\" ]; then printf '%s\\n' \"$path\"; fi; "
            "done"
        ).format(glob_expr=glob_expr)
        result = self._run_device_shell_run_as(package_name, command)
        if result is None or result.returncode != 0:
            return []
        return sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])

    def _pull_run_as_file(self, package_name, remote_file, local_trace_dir):
        local_file = os.path.abspath(os.path.join(local_trace_dir, os.path.basename(remote_file)))
        timeout_sec = self._trace_cmd_timeout()
        cmd = self._adb_base() + [
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
            stdout=self._as_text(result.stdout),
            stderr=self._as_text(result.stderr),
        )

    def _read_remote_text(self, remote_path, require_root=False):
        if not remote_path:
            return ""
        result = self._run_device_shell(
            "cat %s 2>/dev/null" % shlex.quote(remote_path),
            require_root=require_root,
        )
        if result is None or result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def _trace_sidecar_paths(self, remote_prefix):
        normalized = (remote_prefix or "").rstrip("/")
        remote_dir = os.path.dirname(normalized) or "."
        remote_name = os.path.basename(normalized)
        meta_prefix = "%s/.autodroid_trace_meta_%s" % (remote_dir, self._sanitize_name(remote_name))
        return {"stderr": meta_prefix + ".stderr"}

    def _run_as_trace_dir(self, package_name):
        return "/data/user/0/%s/files/autodroid_guard" % package_name

    def _unique_numeric_pids(self, values):
        seen = set()
        ordered = []
        for value in values:
            value = str(value).strip()
            if value.isdigit() and value not in seen:
                ordered.append(value)
                seen.add(value)
        return ordered

    def _extract_pids_from_text(self, package_name, raw_text):
        if not raw_text:
            return []

        pids = []
        regex_patterns = [
            r"(\d+):%s(?:[/:][^\s}]+)?" % re.escape(package_name),
            r"pid=(\d+)\b.*?%s" % re.escape(package_name),
            r"\bProcessRecord\{[^}]*\b(\d+):%s(?:[/:][^\s}]+)?" % re.escape(package_name),
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
        return self._unique_numeric_pids(pids)

    def _query_package_pids(self, package_name):
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
            result = self._run_device_shell(command, require_root=require_root)
            if result is None or result.returncode != 0:
                continue
            pids = self._extract_pids_from_text(package_name, result.stdout)
            if pids:
                return pids
        return []

    def _resolve_wrap_package(self, app=None, packages=None):
        configured = (_get_first_env("AUTODROID_GUARD_TRACE_WRAP_PACKAGE", "DEVICE_TRACE_WRAP_PACKAGE", "GUARD_TRACE_WRAP_PACKAGE", default="") or "").strip()
        if configured:
            return configured
        guard_allowed = (_get_first_env("AUTODROID_GUARD_ALLOWED_PACKAGE", "GUARD_ALLOWED_PACKAGE", "PKG", default="") or "").strip()
        if guard_allowed:
            return guard_allowed
        if app is not None and getattr(app, "get_package_name", None):
            package_name = app.get_package_name()
            if package_name:
                return package_name
        configured_trace_packages = (_get_first_env("AUTODROID_GUARD_TRACE_PACKAGES", "DEVICE_TRACE_PACKAGES", "GUARD_TRACE_PACKAGES", default="") or "").strip()
        if configured_trace_packages:
            for item in configured_trace_packages.split(","):
                item = item.strip()
                if item:
                    return item
        for package_name in self._unique_keep_order(packages or []):
            if package_name:
                return package_name
        return ""

    def _resolve_wrap_activity(self, app=None, package_name=""):
        configured = (_get_first_env("AUTODROID_GUARD_TRACE_WRAP_ACTIVITY", "DEVICE_TRACE_WRAP_ACTIVITY", "GUARD_TRACE_WRAP_ACTIVITY", default="") or "").strip()
        if configured:
            return configured
        if app is not None and getattr(app, "get_main_activity", None):
            main_activity = app.get_main_activity()
            if main_activity:
                if "/" in main_activity:
                    return main_activity
                if package_name:
                    return "%s/%s" % (package_name, main_activity)
                return main_activity
        return ""

    def _wrap_trace_paths(self, package_name):
        trace_dir = self._run_as_trace_dir(package_name)
        return {
            "trace_dir": trace_dir,
            "trace_prefix": trace_dir.rstrip("/") + "/trace.",
            "wrap_log": trace_dir.rstrip("/") + "/wrap.log",
        }

    def _write_temp_script(self, contents):
        temp_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8")
        try:
            temp_file.write(contents)
            temp_file.flush()
        finally:
            temp_file.close()
        return temp_file.name

    def _push_host_file(self, host_path, remote_path):
        return self._run_host_cmd(
            self._adb_base() + ["push", host_path, remote_path],
            timeout_sec=max(self._trace_cmd_timeout(), 30),
        )

    def _build_wrap_script(self, device_trace_tool, trace_dir):
        trace_string_size = int(_get_first_env("AUTODROID_GUARD_TRACE_STRING_SIZE", "DEVICE_TRACE_STRING_SIZE", default="256"))
        return "\n".join(
            [
                "#!/system/bin/sh",
                "TRACE_DIR=%s" % trace_dir,
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
                '%s -f -tt -s %d -e trace=%s -o "$TRACE_FILE" -- "$@" >>"$TRACE_STDOUT" 2>>"$TRACE_STDERR"'
                % (device_trace_tool, trace_string_size, self._trace_filter()),
                "",
                "status=$?",
                'echo "strace_exit=$status" >> "$TRACE_STATUS"',
                "exit $status",
                "",
            ]
        )

    def prepare_runtime(self, app=None, packages=None):
        if self._trace_mode() != "wrap":
            return None
        package_name = self._resolve_wrap_package(app=app, packages=packages or [])
        activity_name = self._resolve_wrap_activity(app=app, package_name=package_name)
        if self._wrap_runtime and self._wrap_runtime.get("ready"):
            runtime_package = self._wrap_runtime.get("package")
            runtime_activity = self._wrap_runtime.get("activity")
            if runtime_package == package_name and runtime_activity == activity_name:
                return self._wrap_runtime
            self.cleanup_runtime()

        if not package_name:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "AUTODROID_GUARD_TRACE_MODE=wrap requires AUTODROID_GUARD_TRACE_WRAP_PACKAGE, AUTODROID_GUARD_ALLOWED_PACKAGE, or a detectable package."}],
            }
            return self._wrap_runtime
        if not activity_name:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "AUTODROID_GUARD_TRACE_MODE=wrap requires AUTODROID_GUARD_TRACE_WRAP_ACTIVITY or a detectable main activity."}],
            }
            return self._wrap_runtime
        host_trace_tool = (_get_first_env("AUTODROID_GUARD_TRACE_WRAP_STRACE_HOST", "DEVICE_TRACE_WRAP_STRACE_HOST", default="") or "").strip()
        if not host_trace_tool or not os.path.isfile(host_trace_tool):
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "AUTODROID_GUARD_TRACE_MODE=wrap requires AUTODROID_GUARD_TRACE_WRAP_STRACE_HOST to point to a valid Android strace binary."}],
            }
            return self._wrap_runtime
        if not self._can_run_as_package(package_name):
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "AUTODROID_GUARD_TRACE_MODE=wrap requires run-as access, but run-as is unavailable for %s." % package_name}],
            }
            return self._wrap_runtime
        if not self._ensure_adb_root():
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "AUTODROID_GUARD_TRACE_MODE=wrap requires adb root on this device, but adb root is unavailable."}],
            }
            return self._wrap_runtime

        device_trace_tool = (_get_first_env("AUTODROID_GUARD_TRACE_WRAP_STRACE_DEVICE", "DEVICE_TRACE_WRAP_STRACE_DEVICE", default="/data/local/tmp/strace_new") or "/data/local/tmp/strace_new").strip()
        wrapper_device_path = (_get_first_env("AUTODROID_GUARD_TRACE_WRAP_SCRIPT_DEVICE", "DEVICE_TRACE_WRAP_SCRIPT_DEVICE", default="/data/local/tmp/autodroid_wrap_trace.sh") or "/data/local/tmp/autodroid_wrap_trace.sh").strip()
        self._run_device_shell("setenforce 0", require_root=True)

        push_result = self._push_host_file(host_trace_tool, device_trace_tool)
        if push_result.returncode != 0:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to push AUTODROID_GUARD_TRACE_WRAP_STRACE_HOST: %s" % (push_result.stderr or push_result.stdout)}],
            }
            return self._wrap_runtime
        self._run_device_shell("chmod 755 %s" % shlex.quote(device_trace_tool), require_root=True)

        paths = self._wrap_trace_paths(package_name)
        wrapper_script = self._build_wrap_script(device_trace_tool, paths["trace_dir"])
        local_wrapper_path = self._write_temp_script(wrapper_script)
        try:
            wrapper_push_result = self._push_host_file(local_wrapper_path, wrapper_device_path)
        finally:
            try:
                os.unlink(local_wrapper_path)
            except OSError:
                pass
        if wrapper_push_result.returncode != 0:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to push wrap script: %s" % (wrapper_push_result.stderr or wrapper_push_result.stdout)}],
            }
            return self._wrap_runtime
        self._run_device_shell("chmod 755 %s" % shlex.quote(wrapper_device_path), require_root=True)

        prepare_result = self._run_device_shell_run_as(
            package_name,
            "mkdir -p files/autodroid_guard && rm -f files/autodroid_guard/*",
        )
        if prepare_result is None or prepare_result.returncode != 0:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to prepare the app-private trace directory via run-as for %s." % package_name}],
            }
            return self._wrap_runtime

        property_value = "/system/bin/sh %s" % wrapper_device_path
        setprop_result = self._run_device_shell(
            "setprop wrap.%s %s" % (package_name, shlex.quote(property_value)),
            require_root=True,
        )
        if setprop_result is None or setprop_result.returncode != 0:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to set wrap.%s on the device." % package_name}],
            }
            return self._wrap_runtime

        if not self._manual_app_launch_mode():
            self._run_device_shell("am force-stop %s" % shlex.quote(package_name), require_root=False)
            launch_result = self._run_device_shell(
                "am start -W -n %s" % shlex.quote(activity_name),
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
                self._wrap_runtime = {
                    "ready": False,
                    "package": package_name,
                    "activity": activity_name,
                    "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to relaunch %s under wrap-mode strace. am start output: %s" % (activity_name, launch_output.strip())}],
                }
                return self._wrap_runtime

            launch_wait = float(_get_first_env("AUTODROID_GUARD_TRACE_WRAP_LAUNCH_WAIT", "DEVICE_TRACE_WRAP_LAUNCH_WAIT", default="3.0"))
            if launch_wait > 0:
                time.sleep(launch_wait)
        else:
            launch_wait = float(_get_first_env("AUTODROID_GUARD_TRACE_WRAP_LAUNCH_WAIT", "DEVICE_TRACE_WRAP_LAUNCH_WAIT", default="3.0"))
            if launch_wait > 0:
                time.sleep(launch_wait)

        self._wrap_runtime = {
            "ready": True,
            "package": package_name,
            "activity": activity_name,
            "property_value": property_value,
            "device_trace_tool": device_trace_tool,
            "wrapper_device_path": wrapper_device_path,
            "remote_trace_dir": paths["trace_dir"],
            "remote_trace_prefix": paths["trace_prefix"],
            "remote_wrap_log": paths["wrap_log"],
            "start_failures": [],
            "prepared_at": int(time.time()),
        }
        return self._wrap_runtime

    def cleanup_runtime(self):
        if self._trace_mode() != "wrap":
            self._wrap_runtime = None
            return None
        runtime = self._wrap_runtime
        if not runtime:
            return None
        package_name = runtime.get("package", "")
        if package_name:
            self._run_device_shell("setprop wrap.%s ''" % package_name, require_root=True)
            if _env_flag("AUTODROID_GUARD_TRACE_WRAP_FORCE_STOP_ON_CLEANUP", False):
                self._run_device_shell("am force-stop %s" % shlex.quote(package_name), require_root=False)
        self._wrap_runtime = None
        return True

    def set_trace_context(self, trace_dir, trace_name, packages=None, app=None):
        if self._trace_mode() == "wrap":
            self.trace_context = self._start_trace_wrap(trace_dir, trace_name, packages or [], app=app)
        else:
            self.trace_context = self._start_trace_attach(trace_dir, trace_name, packages or [])
        self.last_trace_info = None
        return self.trace_context

    def stop_trace(self):
        if not self.trace_context:
            return None
        if self.trace_context.get("trace_mode") == "wrap":
            self.last_trace_info = self._stop_trace_wrap(self.trace_context)
        else:
            self.last_trace_info = self._stop_trace_attach(self.trace_context)
        return self.last_trace_info

    def clear_trace_context(self):
        self.trace_context = None

    def get_last_trace(self):
        return self.last_trace_info

    def _remote_file_size_run_as(self, package_name, remote_file):
        command = "if [ -f {path} ]; then wc -c < {path}; else echo 0; fi".format(path=shlex.quote(remote_file))
        result = self._run_device_shell_run_as(package_name, command)
        if result is None or result.returncode != 0:
            return 0
        text = (result.stdout or "").strip()
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else 0

    def _extract_file_delta(self, local_snapshot_path, start_offset, local_output_path):
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

    def _start_trace_wrap(self, trace_dir, trace_name, packages, app=None):
        os.makedirs(trace_dir, exist_ok=True)
        metadata = self._trace_metadata(mode="wrap")
        runtime = self.prepare_runtime(app=app, packages=packages)
        package_name = self._resolve_wrap_package(app=app, packages=packages)
        packages_seen = self._unique_keep_order(([package_name] if package_name else []) + list(packages or []))
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
        }
        if not runtime or not runtime.get("ready"):
            return {
                **base_payload,
                "start_failures": [] if not runtime else runtime.get("start_failures", []),
            }

        remote_trace_files = self._list_remote_trace_files_run_as(runtime["package"], runtime["remote_trace_prefix"])
        baseline_offsets = {}
        for remote_file in remote_trace_files:
            baseline_offsets[remote_file] = self._remote_file_size_run_as(runtime["package"], remote_file)

        return {
            **base_payload,
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

    def _stop_trace_wrap(self, session):
        stop_delay = float(_get_first_env("AUTODROID_GUARD_TRACE_STOP_DELAY", "DEVICE_TRACE_STOP_DELAY", default="1.0"))
        if stop_delay > 0:
            time.sleep(stop_delay)

        runtime = self._wrap_runtime
        metadata = self._trace_metadata(mode="wrap")
        if not runtime or not runtime.get("ready"):
            return {
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

        local_trace_dir = session.get("local_trace_dir", "")
        os.makedirs(local_trace_dir, exist_ok=True)
        remote_trace_files = self._list_remote_trace_files_run_as(runtime["package"], runtime["remote_trace_prefix"])
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
            current_size = self._remote_file_size_run_as(runtime["package"], remote_file)
            if current_size < start_offset:
                start_offset = 0
            if current_size <= start_offset:
                continue
            local_snapshot_path, pull_result = self._pull_run_as_file(runtime["package"], remote_file, temp_pull_dir)
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
                os.path.join(local_trace_dir, "%s__%s" % (self._sanitize_name(session.get("trace_name", "trace")), os.path.basename(remote_file)))
            )
            if self._extract_file_delta(local_snapshot_path, start_offset, local_delta_path):
                trace_files.append(local_delta_path)
            try:
                os.remove(local_snapshot_path)
            except OSError:
                pass

        return {
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

    def _start_trace_attach(self, trace_dir, trace_name, packages):
        os.makedirs(trace_dir, exist_ok=True)
        metadata = self._trace_metadata(mode="attach")
        trace_tool = metadata["trace_tool"]
        trace_string_size = int(_get_first_env("AUTODROID_GUARD_TRACE_STRING_SIZE", "DEVICE_TRACE_STRING_SIZE", default="256"))
        remote_dir = _get_first_env("AUTODROID_GUARD_TRACE_REMOTE_DIR", "DEVICE_TRACE_REMOTE_DIR", default="/data/local/tmp/autodroid_guard")
        targets = []
        missing_packages = []
        start_failures = []

        for package_name in self._unique_keep_order(packages or []):
            pids = self._query_package_pids(package_name)
            if not pids:
                missing_packages.append(package_name)
                continue
            for pid in pids:
                use_run_as = self._can_run_as_package(package_name)
                package_remote_dir = self._run_as_trace_dir(package_name) if use_run_as else remote_dir
                remote_prefix = "%s/%s_%s" % (
                    package_remote_dir.rstrip("/"),
                    self._sanitize_name(trace_name),
                    self._sanitize_name("%s_%s" % (package_name, pid)),
                )
                remote_glob_expr = self._remote_glob_expr(remote_prefix)
                sidecar_paths = self._trace_sidecar_paths(remote_prefix)
                target_started = False
                for filter_candidate in metadata["trace_filter_candidates"]:
                    trace_command = (
                        "mkdir -p {remote_dir} && "
                        "rm -f {remote_glob_expr} {trace_stderr} && "
                        "{trace_tool} -ff -tt -s {trace_string_size} -e trace={trace_filter} "
                        "-p {pid} -o {remote_prefix} >/dev/null 2>{trace_stderr} & "
                        "tracer_pid=$! && echo $tracer_pid && sleep 0.2 && kill -0 $tracer_pid >/dev/null 2>&1"
                    ).format(
                        remote_dir=shlex.quote(package_remote_dir),
                        remote_glob_expr=remote_glob_expr,
                        trace_stderr=shlex.quote(sidecar_paths["stderr"]),
                        trace_tool=shlex.quote(trace_tool),
                        trace_string_size=trace_string_size,
                        trace_filter=filter_candidate,
                        pid=pid,
                        remote_prefix=shlex.quote(remote_prefix),
                    )
                    if use_run_as:
                        result = self._run_device_shell_run_as(package_name, trace_command)
                    else:
                        result = self._run_device_shell(trace_command, require_root=True)
                    tracer_pid = ""
                    if result is not None and (result.stdout or "").strip():
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
                        stderr_result = self._run_device_shell_run_as(
                            package_name,
                            "cat %s 2>/dev/null" % shlex.quote(sidecar_paths["stderr"]),
                        )
                        stderr_text = ""
                        if stderr_result is not None and stderr_result.returncode == 0:
                            stderr_text = (stderr_result.stdout or "").strip()
                    else:
                        stderr_text = self._read_remote_text(sidecar_paths["stderr"], require_root=True)
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

        return {
            **metadata,
            "packages": self._unique_keep_order(packages or []),
            "missing_packages": missing_packages,
            "start_failures": start_failures,
            "targets": targets,
            "local_trace_dir": os.path.abspath(trace_dir),
            "remote_trace_dir": remote_dir,
            "captured_at": int(time.time()),
            "trace_name": trace_name,
        }

    def _stop_trace_attach(self, session):
        if not session:
            return None
        stop_delay = float(_get_first_env("AUTODROID_GUARD_TRACE_STOP_DELAY", "DEVICE_TRACE_STOP_DELAY", default="1.0"))
        if stop_delay > 0:
            time.sleep(stop_delay)

        targets = session.get("targets", [])
        for target in targets:
            tracer_pid = target.get("tracer_pid", "")
            if tracer_pid:
                self._run_device_shell("kill -INT %s" % tracer_pid, require_root=True)

        trace_files = []
        for target in targets:
            remote_prefix = target["remote_prefix"]
            if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                remote_files = self._list_remote_trace_files_run_as(target["run_as_package"], remote_prefix)
            else:
                remote_files = self._list_remote_trace_files(remote_prefix)
            for remote_file in remote_files:
                if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                    local_file, pull_result = self._pull_run_as_file(target["run_as_package"], remote_file, session["local_trace_dir"])
                else:
                    local_file = os.path.abspath(os.path.join(session["local_trace_dir"], os.path.basename(remote_file)))
                    pull_result = self._run_host_cmd(self._adb_base() + ["pull", remote_file, session["local_trace_dir"]], timeout_sec=max(self._trace_cmd_timeout(), 30))
                if pull_result.returncode == 0:
                    trace_files.append(local_file)
            if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                self._run_device_shell_run_as(target["run_as_package"], "rm -f %s" % target.get("remote_glob_expr", self._remote_glob_expr(remote_prefix)))
                if target.get("remote_stderr_path"):
                    self._run_device_shell_run_as(target["run_as_package"], "rm -f %s" % shlex.quote(target["remote_stderr_path"]))
            else:
                self._run_device_shell("rm -f %s" % target.get("remote_glob_expr", self._remote_glob_expr(remote_prefix)), require_root=True)
                if target.get("remote_stderr_path"):
                    self._run_device_shell("rm -f %s" % shlex.quote(target["remote_stderr_path"]), require_root=True)

        metadata = self._trace_metadata(mode="attach")
        return {
            **metadata,
            "packages": session.get("packages", []),
            "missing_packages": session.get("missing_packages", []),
            "start_failures": session.get("start_failures", []),
            "targets": targets,
            "trace_files": sorted(trace_files),
            "trace_file_count": len(trace_files),
            "local_trace_dir": session.get("local_trace_dir", ""),
            "remote_trace_dir": session.get("remote_trace_dir", ""),
            "captured_at": int(time.time()),
        }
