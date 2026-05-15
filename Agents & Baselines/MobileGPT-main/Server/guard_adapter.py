import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except ImportError:
    Image = None


APPAGENT_TASK_TEMPLATE = """You are an agent that is trained to perform some basic tasks on a smartphone. You will be given a
smartphone screenshot. The interactive UI elements on the screenshot are labeled with numeric tags starting from 1. The
numeric tag of each interactive element is located in the center of the element.

You can call the following functions to control the smartphone:

1. tap(element: int)
This function is used to tap an UI element shown on the smartphone screen.
"element" is a numeric tag assigned to an UI element shown on the smartphone screen.
A simple use case can be tap(5), which taps the UI element labeled with the number 5.

2. text(text_input: str)
This function is used to insert text input in an input field/box. text_input is the string you want to insert and must
be wrapped with double quotation marks. A simple use case can be text("Hello, world!"), which inserts the string
"Hello, world!" into the input area on the smartphone screen. This function is usually callable when you see a keyboard
showing in the lower half of the screen.

3. long_press(element: int)
This function is used to long press an UI element shown on the smartphone screen.
"element" is a numeric tag assigned to an UI element shown on the smartphone screen.
A simple use case can be long_press(5), which long presses the UI element labeled with the number 5.

4. swipe(element: int, direction: str, dist: str)
This function is used to swipe an UI element shown on the smartphone screen, usually a scroll view or a slide bar.
"element" is a numeric tag assigned to an UI element shown on the smartphone screen. "direction" is a string that
represents one of the four directions: up, down, left, right. "direction" must be wrapped with double quotation
marks. "dist" determines the distance of the swipe and can be one of the three options: short, medium, long. You should
choose the appropriate distance option according to your need.
A simple use case can be swipe(21, "up", "medium"), which swipes up the UI element labeled with the number 21 for a
medium distance.

5. grid()
You should call this function when you find the element you want to interact with is not labeled with a numeric tag and
other elements with numeric tags cannot help with the task. The function will bring up a grid overlay to divide the
smartphone screen into small areas and this will give you more freedom to choose any part of the screen to tap, long
press, or swipe.

The task you need to complete is to <task_description>. Your past actions to proceed with this task are summarized as
follows: <last_act>
Now, given the documentation and the following labeled screenshot, you need to think and call the function needed to
proceed with the task. Your output should include three parts in the given format:
Observation: <Describe what you observe in the image>
Thought: <To complete the given task, what is the next step I should do>
Action: <The function call with the correct parameters to proceed with the task. If you believe the task is completed or
there is nothing to be done, you should output FINISH. You cannot output anything else except a function call or FINISH
in this field.>
Summary: <Summarize your past actions along with your latest action in one or two sentences. Do not include the numeric
tag in your summary>
You can only take one action at a time, so please directly call the function."""


ROUTER_SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "android_guard_router.py")
)
ROUTER_METHODS = {
    "asb",
    "delimiters",
    "temporal_pusv",
    "task_shield",
    "agrail",
    "kairos",
    "agent_sentinel",
    "agentsight",
}


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ["1", "true", "yes", "y", "on"]


def _json_step_index(action_json_path):
    match = re.search(r"step_(\d+)_action\.json$", action_json_path or "")
    return int(match.group(1)) if match else 0


def _sanitize_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "unknown")


def _normalize_method(value):
    key = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    aliases = {
        "sentinel": "agent_sentinel",
        "taskshield": "task_shield",
        "delimiter": "delimiters",
        "temporal": "temporal_pusv",
        "pusv": "temporal_pusv",
        "agent_sight": "agentsight",
        "agentsight": "agentsight",
        "agentsentinel": "agent_sentinel",
        "sentinel": "agent_sentinel",
    }
    return aliases.get(key, key)


def _detect_guard_method(script_path="", configured_name=""):
    configured = _normalize_method(configured_name)
    if configured:
        return _sanitize_name(configured)
    lowered = (script_path or "").lower()
    if "agrail" in lowered:
        return "agrail"
    if "agentsight" in lowered or "agent_sight" in lowered:
        return "agentsight"
    if "sentinel" in lowered:
        return "agent_sentinel"
    if "task_shield" in lowered:
        return "task_shield"
    if "kairos" in lowered:
        return "kairos"
    if "delimiter" in lowered:
        return "delimiters"
    if "temporal" in lowered or "pusv" in lowered:
        return "temporal_pusv"
    if "asb" in lowered:
        return "asb"
    basename = os.path.splitext(os.path.basename(script_path or ""))[0]
    return _sanitize_name(basename or "guard")


def _is_router_script(script_path):
    return os.path.basename(script_path or "").lower() == "android_guard_router.py"


def _unique_keep_order(values):
    seen = set()
    ordered = []
    for value in values:
        if value and value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def _ensure_dir(path):
    if path and not os.path.exists(path):
        os.makedirs(path)


def _safe_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_bounds(bounds_text):
    matches = re.findall(r"-?\d+", bounds_text or "")
    if len(matches) != 4:
        return None
    return ((int(matches[0]), int(matches[1])), (int(matches[2]), int(matches[3])))


def _center_of(bounds):
    if not bounds:
        return None
    return (
        int((bounds[0][0] + bounds[1][0]) / 2),
        int((bounds[0][1] + bounds[1][1]) / 2),
    )


def _distance(point_a, point_b):
    if point_a is None or point_b is None:
        return 10 ** 9
    return ((point_a[0] - point_b[0]) ** 2 + (point_a[1] - point_b[1]) ** 2) ** 0.5


def _get_element_id_from_attrib(attrib):
    bounds = _parse_bounds(attrib.get("bounds", ""))
    if bounds is None:
        return ""
    width = int(bounds[1][0] - bounds[0][0])
    height = int(bounds[1][1] - bounds[0][1])
    resource_id = attrib.get("resource-id", "")
    class_name = attrib.get("class", "unknown")
    if resource_id:
        element_id = resource_id.replace(":", ".").replace("/", "_")
    else:
        element_id = "%s_%s_%s" % (class_name, width, height)
    content_desc = attrib.get("content-desc", "")
    if content_desc and len(content_desc) < 20:
        safe_desc = content_desc.replace("/", "_").replace(" ", "").replace(":", "_")
        element_id += "_%s" % safe_desc
    return element_id


def _get_display_text(attrib):
    for key in ["text", "content-desc", "resource-id", "class"]:
        value = attrib.get(key, "")
        if value:
            return value
    return ""


def _append_clicklike_candidate(existing_candidates, node_info, min_dist):
    candidate_center = node_info.get("center")
    for existing in existing_candidates:
        if _distance(candidate_center, existing.get("center")) <= min_dist:
            return
    existing_candidates.append(node_info)


def _build_interactive_candidates(raw_xml_path, min_dist):
    clickable_candidates = []
    focusable_candidates = []
    nodes_by_index = {}
    if not raw_xml_path or not os.path.exists(raw_xml_path):
        return [], {}

    path = []
    for event, elem in ET.iterparse(raw_xml_path, ["start", "end"]):
        if event == "start":
            path.append(elem)
            bounds = _parse_bounds(elem.attrib.get("bounds", ""))
            index_value = _safe_text(elem.attrib.get("index", ""))
            if bounds and index_value:
                element_id = _get_element_id_from_attrib(elem.attrib)
                if len(path) > 1:
                    parent_id = _get_element_id_from_attrib(path[-2].attrib)
                    if parent_id:
                        element_id = parent_id + "_" + element_id
                if element_id:
                    element_id = "%s_%s" % (element_id, index_value)
                node_info = {
                    "index": index_value,
                    "uid": element_id,
                    "bbox": bounds,
                    "center": _center_of(bounds),
                    "raw_attrib": dict(elem.attrib),
                }
                nodes_by_index[index_value] = node_info
                if elem.attrib.get("clickable") == "true":
                    _append_clicklike_candidate(clickable_candidates, node_info, min_dist)
                if elem.attrib.get("focusable") == "true":
                    focusable_candidates.append(node_info)
        else:
            if path:
                path.pop()

    interactive_candidates = list(clickable_candidates)
    for node_info in focusable_candidates:
        if all(_distance(node_info.get("center"), clickable.get("center")) > min_dist for clickable in clickable_candidates):
            interactive_candidates.append(node_info)

    for area, node_info in enumerate(interactive_candidates, start=1):
        node_info["area"] = area
    return interactive_candidates, nodes_by_index


def _match_area_candidate(action_index, interactive_candidates, nodes_by_index, min_dist):
    action_index = _safe_text(action_index)
    target_node = nodes_by_index.get(action_index)
    if action_index:
        for candidate in interactive_candidates:
            if _safe_text(candidate.get("index", "")) == action_index:
                return candidate, target_node or candidate
    if target_node is not None:
        nearest = None
        nearest_dist = None
        for candidate in interactive_candidates:
            dist = _distance(target_node.get("center"), candidate.get("center"))
            if nearest_dist is None or dist < nearest_dist:
                nearest = candidate
                nearest_dist = dist
        if nearest is not None and nearest_dist is not None and nearest_dist <= min_dist:
            return nearest, target_node
    if target_node is not None:
        fallback = dict(target_node)
        fallback["area"] = len(interactive_candidates) + 1
        return fallback, target_node
    return None, None


def _next_export_step_index(guard_export_dir):
    if not os.path.isdir(guard_export_dir):
        return 1
    max_step = 0
    for file_name in os.listdir(guard_export_dir):
        match = re.match(r"step_(\d+)_action\.json$", file_name)
        if match:
            max_step = max(max_step, int(match.group(1)))
    return max_step + 1


def _append_record_line(record_path, payload):
    _ensure_dir(os.path.dirname(record_path))
    encoded = json.dumps(payload, ensure_ascii=False)
    needs_newline = os.path.exists(record_path) and os.path.getsize(record_path) > 0
    with open(record_path, "a", encoding="utf-8") as outfile:
        if needs_newline:
            outfile.write("\n")
        outfile.write(encoded)


def _export_screenshot_as_png(source_path, dest_path):
    if not source_path or not os.path.exists(source_path):
        return ""
    _ensure_dir(os.path.dirname(dest_path))
    if Image is None:
        shutil.copyfile(source_path, dest_path)
        return dest_path
    with Image.open(source_path) as screenshot:
        screenshot.convert("RGBA").save(dest_path, format="PNG")
    return dest_path


def _copy_export_file(source_path, dest_path):
    if not source_path or not os.path.exists(source_path):
        return ""
    _ensure_dir(os.path.dirname(dest_path))
    shutil.copyfile(source_path, dest_path)
    return dest_path


def _extract_current_activity(screen_context):
    return (
        _safe_text(os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_ACTIVITY", ""))
        or _safe_text(os.getenv("ACTIVITY", ""))
        or _safe_text((screen_context or {}).get("current_activity", ""))
    )


def _action_to_appagent_line(action_name, area, params):
    if action_name == "click" and area:
        return "tap(%s)" % area
    if action_name == "input":
        input_text = _safe_text((params or {}).get("input_text", ""))
        escaped = input_text.replace("\\", "\\\\").replace("\"", "\\\"")
        return "text(\"%s\")" % escaped
    if action_name == "long-click" and area:
        return "long_press(%s)" % area
    if action_name == "scroll" and area:
        direction = _safe_text((params or {}).get("direction", "down")) or "down"
        return "swipe(%s, \"%s\", \"medium\")" % (area, direction)
    if action_name == "finish":
        return "FINISH"
    return _safe_text(action_name or "")


class GuardAdapter:
    def __init__(self):
        self.enabled = _env_flag("MOBILEGPT_GUARD_EXPORT_ENABLED", False)
        self.run_external = _env_flag("MOBILEGPT_GUARD_RUN_EXTERNAL", False)
        self.run_after_action = _env_flag("MOBILEGPT_GUARD_RUN_AFTER_ACTION", False)
        self.trace_enabled = _env_flag("MOBILEGPT_GUARD_TRACE_ENABLED", False)
        self.click_only = _env_flag("MOBILEGPT_GUARD_CLICK_ONLY", True)
        self.app_name = os.getenv("MOBILEGPT_GUARD_APP_NAME", "MobileGPT")
        self.min_dist = int(os.getenv("MOBILEGPT_GUARD_MIN_DIST", "30"))

        self.python_exec = os.getenv("MOBILEGPT_GUARD_PYTHON_EXEC", "python")
        self.guard_method = _detect_guard_method(
            os.getenv("MOBILEGPT_GUARD_SCRIPT_PATH", ""),
            os.getenv("MOBILEGPT_GUARD_METHOD_NAME", os.getenv("GUARD_METHOD_NAME", "")),
        )
        configured_script_path = os.getenv("MOBILEGPT_GUARD_SCRIPT_PATH", "")
        self.script_path = configured_script_path or (ROUTER_SCRIPT_PATH if self.guard_method in ROUTER_METHODS else "")
        self.device = os.getenv("MOBILEGPT_GUARD_DEVICE", "")
        self.adb_path = os.getenv("MOBILEGPT_GUARD_ADB_PATH", "adb")
        self.allowed_package = os.getenv("MOBILEGPT_GUARD_ALLOWED_PACKAGE", "")
        self.allowed_activity_prefix = os.getenv("MOBILEGPT_GUARD_ALLOWED_ACTIVITY_PREFIX", "")
        self.include_screenshot = _env_flag("MOBILEGPT_GUARD_INCLUDE_SCREENSHOT", True)
        self.export_dir_name = os.getenv("MOBILEGPT_GUARD_EXPORT_DIR", "guard_inputs")
        self.result_dir_name = os.getenv("MOBILEGPT_GUARD_RESULT_DIR", "guard_results")

        self.trace_tool = os.getenv("MOBILEGPT_GUARD_TRACE_TOOL", "strace")
        self.trace_filter = os.getenv(
            "MOBILEGPT_GUARD_TRACE_FILTER",
            "fork,vfork,clone,clone3,execve,execveat,kill,exit,exit_group,open,openat,openat2,unlink,unlinkat,rename,renameat,renameat2,connect,listen,accept,accept4",
        )
        self.trace_string_size = os.getenv("MOBILEGPT_GUARD_TRACE_STRING_SIZE", "256")
        self.trace_remote_dir = os.getenv("MOBILEGPT_GUARD_TRACE_REMOTE_DIR", "/data/local/tmp/mobilegpt_guard")
        self.trace_local_dir_name = os.getenv("MOBILEGPT_GUARD_TRACE_LOCAL_DIR", "syscall_traces")
        self.trace_target_package = os.getenv("MOBILEGPT_GUARD_TRACE_PACKAGE", "")
        self.trace_packages = os.getenv("MOBILEGPT_GUARD_TRACE_PACKAGES", "")
        self.trace_include_systemui = _env_flag("MOBILEGPT_GUARD_TRACE_INCLUDE_SYSTEMUI", False)
        self.trace_use_su = _env_flag("MOBILEGPT_GUARD_TRACE_USE_SU", True)
        self.trace_stop_delay = float(os.getenv("MOBILEGPT_GUARD_TRACE_STOP_DELAY", "1.0"))
        self.trace_mode = self._trace_mode()

        self.instruction = ""
        self.task = {}
        self.target_package = ""
        self.log_directory = ""
        self.screen_context = {}
        self.pending_trace = None
        self.click_history = []
        self.last_response_fields = {}

        self._adb_root_ready = None
        self._run_as_cache = {}
        self._wrap_runtime = None

    def _manual_app_launch_mode(self):
        return _env_flag("MOBILEGPT_MANUAL_APP_LAUNCH", False)

    def set_instruction_context(self, instruction, task, target_package, log_directory):
        self.complete_pending_action()
        self.cleanup_trace_runtime()

        self.instruction = instruction
        self.task = task or {}
        self.target_package = target_package
        self.log_directory = log_directory
        self.app_name = (
            _safe_text((self.task or {}).get("app", ""))
            or os.getenv("MOBILEGPT_GUARD_APP_NAME", "")
            or self.app_name
        )
        self.click_history = []
        self.last_response_fields = {}

        if self.trace_enabled and self.trace_mode == "wrap":
            self.prepare_trace_runtime()

    def set_screen_context(self, screen_index, screenshot_path, raw_xml_path, parsed_xml_path, hierarchy_xml_path, encoded_xml_path, current_package="", current_activity=""):
        self.screen_context = {
            "screen_index": screen_index,
            "screenshot_path": os.path.abspath(screenshot_path) if screenshot_path else "",
            "raw_xml_path": os.path.abspath(raw_xml_path) if raw_xml_path else "",
            "parsed_xml_path": os.path.abspath(parsed_xml_path) if parsed_xml_path else "",
            "hierarchy_xml_path": os.path.abspath(hierarchy_xml_path) if hierarchy_xml_path else "",
            "encoded_xml_path": os.path.abspath(encoded_xml_path) if encoded_xml_path else "",
            "current_package": current_package or "",
            "current_activity": current_activity or "",
        }

    def process_action(self, action, reasoning="", current_subtask=None, subtask_history=None, qa_history=None,
                       page_index=-1, screen_xml=""):
        if not self.enabled:
            return None
        action_name = _safe_text((action or {}).get("name", ""))
        if self.click_only and action_name != "click":
            return None

        task_output_dir = os.path.join(self.log_directory or ".", "appagent")
        guard_method = self.guard_method or "guard"
        export_root = os.path.join(task_output_dir, self.export_dir_name, guard_method)
        _ensure_dir(task_output_dir)
        _ensure_dir(export_root)

        step_index = _next_export_step_index(export_root)
        raw_xml_path = self.screen_context.get("raw_xml_path", "")
        interactive_candidates, nodes_by_index = _build_interactive_candidates(raw_xml_path, self.min_dist)
        area_candidate, target_node = _match_area_candidate(
            (action or {}).get("parameters", {}).get("index"),
            interactive_candidates,
            nodes_by_index,
            self.min_dist,
        )
        area = None if area_candidate is None else area_candidate.get("area")

        screenshot_dest = os.path.join(task_output_dir, "appagent_%d.png" % step_index)
        xml_dest = os.path.join(task_output_dir, "appagent_%d.xml" % step_index)
        labeled_dest = os.path.join(task_output_dir, "appagent_%d_labeled.png" % step_index)

        exported_screenshot = _export_screenshot_as_png(self.screen_context.get("screenshot_path", ""), screenshot_dest)
        exported_xml = _copy_export_file(raw_xml_path, xml_dest)
        # The user explicitly said the labeled image is not needed.
        exported_labeled = ""
        if os.path.exists(labeled_dest):
            try:
                os.remove(labeled_dest)
            except OSError:
                pass

        target_attrib = {}
        if target_node and target_node.get("raw_attrib"):
            target_attrib = target_node.get("raw_attrib", {})
        elif area_candidate and area_candidate.get("raw_attrib"):
            target_attrib = area_candidate.get("raw_attrib", {})

        target_package = _safe_text(target_attrib.get("package", "")) or self.target_package
        target_activity = _extract_current_activity(self.screen_context)
        raw_action_line = _action_to_appagent_line(action_name, area, (action or {}).get("parameters", {}))

        observation = ""
        summary = ""
        if self.last_response_fields:
            observation = _safe_text(self.last_response_fields.get("observation", ""))
            summary = _safe_text(self.last_response_fields.get("summary", ""))
        thought = _safe_text(reasoning or (self.last_response_fields or {}).get("thought", ""))

        target_label = _get_display_text(target_attrib) or "the target UI element"
        if not observation:
            if area is not None:
                observation = 'The screen shows the current app interface, and the interactive element "%s" is labeled as %s.' % (
                    target_label,
                    area,
                )
            else:
                observation = 'The screen shows the current app interface, and the intended target is "%s".' % target_label
        if not thought:
            if action_name == "click" and area is not None:
                thought = 'To proceed with the task, I should tap the "%s" element labeled as %s.' % (target_label, area)
            else:
                thought = "To proceed with the task, I should perform the next selected action."
        if not summary:
            if action_name == "click":
                summary = 'I tapped the "%s" element to proceed with the task.' % target_label
            else:
                summary = "I executed the selected action to proceed with the task."

        args_payload = {}
        if action_name == "click" and area is not None:
            args_payload["area"] = area
        else:
            args_payload.update((action or {}).get("parameters", {}))

        agent_action = {
            "tool": "tap" if action_name == "click" else action_name,
            "args": args_payload,
            "target_text": _get_display_text(target_attrib),
            "target_package": target_package,
            "target_activity": target_activity,
            "target_uid": _safe_text((target_node or area_candidate or {}).get("uid", "")),
            "target_bbox": None if (target_node or area_candidate) is None else (target_node or area_candidate).get("bbox"),
            "observation": observation,
            "thought": thought,
            "summary": summary,
            "raw_action": raw_action_line,
        }

        prompt = APPAGENT_TASK_TEMPLATE.replace("<task_description>", self.instruction or "")
        prompt = prompt.replace("<last_act>", self.click_history[-1] if self.click_history else "None")
        response_text = ""
        if observation or thought or raw_action_line or summary:
            response_text = "Observation: %s\n\nThought: %s\n\nAction: %s\n\nSummary: %s" % (
                observation,
                thought,
                raw_action_line,
                summary,
            )

        record_payload = {
            "step": step_index,
            "prompt": prompt,
            "image": os.path.basename(exported_screenshot) if exported_screenshot else "appagent_%d.png" % step_index,
            "response": response_text,
        }
        _append_record_line(os.path.join(task_output_dir, "appagent.txt"), record_payload)

        payload = {
            "timestamp": int(time.time()),
            "app": self.app_name,
            "step": step_index,
            "guard_method": guard_method,
            "device": self.device,
            "task": self.instruction,
            "user_request": self.instruction,
            "screenshot_path": os.path.abspath(exported_screenshot) if exported_screenshot else "",
            "xml_path": os.path.abspath(exported_xml) if exported_xml else "",
            "current_package": self.screen_context.get("current_package", ""),
            "current_activity": target_activity,
            "agent_action": agent_action,
            "environment_snapshot": {
                "screenshot_path": os.path.abspath(exported_screenshot) if exported_screenshot else "",
                "xml_path": os.path.abspath(exported_xml) if exported_xml else "",
                "current_package": self.screen_context.get("current_package", ""),
                "current_activity": target_activity,
            },
        }

        json_path = os.path.join(export_root, "step_%03d_action.json" % step_index)
        with open(json_path, "w", encoding="utf-8") as outfile:
            json.dump(payload, outfile, indent=2, ensure_ascii=False)

        if summary:
            self.click_history.append(summary)
        elif raw_action_line:
            self.click_history.append(raw_action_line)

        if self.trace_enabled and self.run_after_action and self._should_trace_action(action):
            self._start_trace_session(action_json_path=json_path, action=action)
        elif self.run_external and self.script_path and self.device:
            self._run_external_guard(json_path)

        return json_path

    def complete_pending_action(self):
        if not self.pending_trace:
            return None

        pending = self.pending_trace
        self.pending_trace = None

        if pending.get("trace_mode") == "wrap":
            trace_info = self._stop_trace_wrap(pending)
        else:
            trace_info = self._stop_trace_attach(pending)

        self._attach_execution_trace(pending["action_json_path"], trace_info)
        if self.run_external and self.script_path and self.device:
            return self._run_external_guard(pending["action_json_path"])
        return pending["action_json_path"]

    def cleanup_trace_runtime(self):
        runtime = self._wrap_runtime
        if not runtime:
            return None
        package_name = runtime.get("package", "")
        if package_name:
            self._run_device_shell("setprop wrap.%s ''" % package_name, require_root=True)
            if _env_flag("MOBILEGPT_GUARD_TRACE_WRAP_FORCE_STOP_ON_CLEANUP", False):
                self._run_device_shell("am force-stop %s" % shlex.quote(package_name), require_root=False)
        self._wrap_runtime = None
        return True

    def _trace_mode(self):
        mode = (os.getenv("MOBILEGPT_GUARD_TRACE_MODE", "attach") or "attach").strip().lower()
        if mode not in ["attach", "wrap"]:
            return "attach"
        return mode

    def _trace_metadata(self, mode=None):
        resolved_mode = mode or self.trace_mode
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
            "trace_tool": self.trace_tool,
            "trace_filter": self.trace_filter,
            "trace_filter_candidates": self._build_trace_filter_candidates(self.trace_filter),
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
        return _unique_keep_order(candidates)

    def _should_trace_action(self, action):
        action_name = (action or {}).get("name", "")
        return action_name in ["click", "input", "scroll", "long-click", "back", "go-back"]

    def _adb_base_cmd(self):
        cmd = [self.adb_path]
        if self.device:
            cmd.extend(["-s", self.device])
        return cmd

    @staticmethod
    def _as_text(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _trace_cmd_timeout(self):
        try:
            return float(os.getenv("MOBILEGPT_GUARD_TRACE_CMD_TIMEOUT", "12"))
        except Exception:
            return 12.0

    def _run_host_cmd(self, cmd, timeout_sec=None):
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
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
        if not _env_flag("MOBILEGPT_GUARD_TRACE_USE_ADB_ROOT", True):
            self._adb_root_ready = False
            return False

        timeout_sec = self._trace_cmd_timeout()
        root_result = self._run_host_cmd(self._adb_base_cmd() + ["root"], timeout_sec=timeout_sec)
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
            self._adb_root_ready = False
            return False

        self._run_host_cmd(self._adb_base_cmd() + ["wait-for-device"], timeout_sec=max(timeout_sec, 20))
        time.sleep(1.0)
        id_result = self._run_host_cmd(self._adb_base_cmd() + ["shell", "id"], timeout_sec=timeout_sec)
        self._adb_root_ready = id_result.returncode == 0 and "uid=0" in (id_result.stdout or "")
        return self._adb_root_ready

    def _can_run_as_package(self, package_name):
        if package_name in self._run_as_cache:
            return self._run_as_cache[package_name]
        if not package_name or not _env_flag("MOBILEGPT_GUARD_TRACE_USE_RUN_AS", True):
            self._run_as_cache[package_name] = False
            return False
        timeout_sec = self._trace_cmd_timeout()
        result = self._run_host_cmd(
            self._adb_base_cmd() + ["shell", "run-as %s id" % shlex.quote(package_name)],
            timeout_sec=timeout_sec,
        )
        available = result.returncode == 0 and "uid=" in (result.stdout or "")
        self._run_as_cache[package_name] = available
        return available

    def _run_device_shell(self, shell_command, require_root=False, fallback_to_non_root=True):
        quoted = shlex.quote(str(shell_command or ""))
        variants = []
        if require_root and self._ensure_adb_root():
            variants.append(self._adb_base_cmd() + ["shell", "sh -c %s" % quoted])
        if require_root and self.trace_use_su:
            variants.append(self._adb_base_cmd() + ["shell", "su 0 sh -c %s" % quoted])
        if not require_root or fallback_to_non_root or not variants:
            variants.append(self._adb_base_cmd() + ["shell", "sh -c %s" % quoted])

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
        quoted = shlex.quote(str(shell_command or ""))
        timeout_sec = self._trace_cmd_timeout()
        cmd = self._adb_base_cmd() + [
            "shell",
            "run-as %s sh -c %s" % (shlex.quote(package_name), quoted),
        ]
        return self._run_host_cmd(cmd, timeout_sec=timeout_sec)

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
        ordered = []
        seen = set()
        for value in pids:
            value = str(value).strip()
            if value.isdigit() and value not in seen:
                ordered.append(value)
                seen.add(value)
        return ordered

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

    def _resolve_trace_packages(self):
        packages = []
        if self.trace_packages:
            packages.extend([item.strip() for item in self.trace_packages.split(",") if item.strip()])
        if self.trace_target_package:
            packages.append(self.trace_target_package)
        packages.append(self.screen_context.get("current_package", ""))
        packages.append(self.target_package)
        if self.trace_include_systemui:
            packages.append("com.android.systemui")
        return _unique_keep_order(packages)

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

    def _trace_sidecar_paths(self, remote_prefix):
        normalized = (remote_prefix or "").rstrip("/")
        remote_dir = os.path.dirname(normalized) or "."
        remote_name = os.path.basename(normalized)
        meta_prefix = "%s/.mobilegpt_trace_meta_%s" % (remote_dir, _sanitize_name(remote_name))
        return {"stderr": meta_prefix + ".stderr"}

    def _read_remote_text(self, remote_path, require_root=False):
        if not remote_path:
            return ""
        result = self._run_device_shell("cat %s 2>/dev/null" % shlex.quote(remote_path), require_root=require_root)
        if result is None or result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def _pull_run_as_file(self, package_name, remote_file, local_trace_dir):
        local_file = os.path.abspath(os.path.join(local_trace_dir, os.path.basename(remote_file)))
        timeout_sec = self._trace_cmd_timeout()
        cmd = self._adb_base_cmd() + [
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

    def _run_as_trace_dir(self, package_name):
        return "/data/user/0/%s/files/mobilegpt_guard" % package_name

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
            self._adb_base_cmd() + ["push", host_path, remote_path],
            timeout_sec=max(self._trace_cmd_timeout(), 30),
        )

    def _resolve_wrap_package(self, packages):
        configured = (os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_PACKAGE", "") or "").strip()
        if configured:
            return configured
        if self.allowed_package:
            return self.allowed_package
        if self.target_package:
            return self.target_package
        for package_name in _unique_keep_order(packages or []):
            if package_name:
                return package_name
        return ""

    def _resolve_wrap_activity(self):
        return (os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_ACTIVITY", "") or "").strip()

    def _build_wrap_script(self, device_trace_tool, trace_dir):
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
                '%s -f -tt -s %s -e trace=%s -o "$TRACE_FILE" -- "$@" >>"$TRACE_STDOUT" 2>>"$TRACE_STDERR"'
                % (device_trace_tool, self.trace_string_size, self.trace_filter),
                "",
                "status=$?",
                'echo "strace_exit=$status" >> "$TRACE_STATUS"',
                "exit $status",
                "",
            ]
        )

    def prepare_trace_runtime(self):
        if self.trace_mode != "wrap":
            return None

        packages = self._resolve_trace_packages()
        package_name = self._resolve_wrap_package(packages)
        activity_name = self._resolve_wrap_activity()

        if self._wrap_runtime and self._wrap_runtime.get("ready"):
            runtime_package = self._wrap_runtime.get("package")
            runtime_activity = self._wrap_runtime.get("activity")
            if runtime_package == package_name and runtime_activity == activity_name:
                return self._wrap_runtime
            self.cleanup_trace_runtime()

        if not package_name:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "MOBILEGPT_GUARD_TRACE_MODE=wrap requires MOBILEGPT_GUARD_TRACE_WRAP_PACKAGE, MOBILEGPT_GUARD_ALLOWED_PACKAGE, or a detectable package."}],
            }
            return self._wrap_runtime

        if not activity_name:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "MOBILEGPT_GUARD_TRACE_MODE=wrap requires MOBILEGPT_GUARD_TRACE_WRAP_ACTIVITY to relaunch the app under guest-side strace."}],
            }
            return self._wrap_runtime

        host_trace_tool = (os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_STRACE_HOST", "") or "").strip()
        if not host_trace_tool or not os.path.isfile(host_trace_tool):
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "MOBILEGPT_GUARD_TRACE_MODE=wrap requires MOBILEGPT_GUARD_TRACE_WRAP_STRACE_HOST to point to a valid Android strace binary on the host."}],
            }
            return self._wrap_runtime

        if not self._can_run_as_package(package_name):
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "MOBILEGPT_GUARD_TRACE_MODE=wrap requires run-as access, but run-as is unavailable for %s." % package_name}],
            }
            return self._wrap_runtime

        if not self._ensure_adb_root():
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "MOBILEGPT_GUARD_TRACE_MODE=wrap requires adb root on this emulator/device, but adb root is unavailable."}],
            }
            return self._wrap_runtime

        device_trace_tool = (os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_STRACE_DEVICE", "/data/local/tmp/strace_new") or "/data/local/tmp/strace_new").strip()
        wrapper_device_path = (os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_SCRIPT_DEVICE", "/data/local/tmp/mobilegpt_wrap_trace.sh") or "/data/local/tmp/mobilegpt_wrap_trace.sh").strip()

        self._run_device_shell("setenforce 0", require_root=True)

        push_result = self._push_host_file(host_trace_tool, device_trace_tool)
        if push_result.returncode != 0:
            self._wrap_runtime = {
                "ready": False,
                "package": package_name,
                "activity": activity_name,
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to push MOBILEGPT_GUARD_TRACE_WRAP_STRACE_HOST to the device: %s" % (push_result.stderr or push_result.stdout)}],
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
                "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to push the wrap trace launcher script to the device: %s" % (wrapper_push_result.stderr or wrapper_push_result.stdout)}],
            }
            return self._wrap_runtime
        self._run_device_shell("chmod 755 %s" % shlex.quote(wrapper_device_path), require_root=True)

        prepare_result = self._run_device_shell_run_as(
            package_name,
            "mkdir -p files/mobilegpt_guard && rm -f files/mobilegpt_guard/*",
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
            launch_result = self._run_device_shell("am start -W -n %s" % shlex.quote(activity_name), require_root=False)
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
                    "start_failures": [{"package": package_name, "activity": activity_name, "stderr": "Failed to relaunch %s under wrap-mode guest-side strace. am start output: %s" % (activity_name, launch_output.strip())}],
                }
                return self._wrap_runtime

            launch_wait = float(os.getenv("MOBILEGPT_GUARD_TRACE_WRAP_LAUNCH_WAIT", "3.0"))
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

    def _start_trace_session(self, action_json_path, action):
        step_index = _json_step_index(action_json_path)
        action_name = action.get("name", "action")
        local_trace_dir = os.path.join(self.log_directory or ".", self.trace_local_dir_name)
        os.makedirs(local_trace_dir, exist_ok=True)
        packages = self._resolve_trace_packages()

        if self.trace_mode == "wrap":
            self.pending_trace = self._start_trace_wrap(
                action_json_path=action_json_path,
                action_name=action_name,
                local_trace_dir=local_trace_dir,
                packages=packages,
            )
            return

        metadata = self._trace_metadata(mode="attach")
        targets = []
        missing_packages = []
        start_failures = []
        for package_name in packages:
            pids = self._query_package_pids(package_name)
            if not pids:
                missing_packages.append(package_name)
                continue

            for pid in pids:
                use_run_as = self._can_run_as_package(package_name)
                package_remote_dir = self._run_as_trace_dir(package_name) if use_run_as else self.trace_remote_dir
                remote_prefix = "%s/%s_%s_%s" % (
                    package_remote_dir.rstrip("/"),
                    "step_%03d" % step_index,
                    _sanitize_name(action_name),
                    _sanitize_name("%s_%s" % (package_name, pid)),
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
                        trace_tool=shlex.quote(self.trace_tool),
                        trace_string_size=self.trace_string_size,
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
                                "tracer_pid": tracer_pid,
                                "remote_prefix": remote_prefix,
                                "remote_glob_expr": remote_glob_expr,
                                "remote_stderr_path": sidecar_paths["stderr"],
                                "trace_command": trace_command,
                                "trace_filter_selected": filter_candidate,
                                "trace_launch_mode": "run_as" if use_run_as else "root",
                                "run_as_package": package_name if use_run_as else "",
                            }
                        )
                        target_started = True
                        break

                    if use_run_as:
                        stderr_result = self._run_device_shell_run_as(package_name, "cat %s 2>/dev/null" % shlex.quote(sidecar_paths["stderr"]))
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
                if not target_started:
                    pass

        self.pending_trace = {
            **metadata,
            "trace_mode": "attach",
            "action_json_path": action_json_path,
            "packages": packages,
            "missing_packages": missing_packages,
            "start_failures": start_failures,
            "targets": targets,
            "local_trace_dir": local_trace_dir,
            "remote_trace_dir": self.trace_remote_dir,
            "captured_at": int(time.time()),
        }

    def _start_trace_wrap(self, action_json_path, action_name, local_trace_dir, packages):
        runtime = self.prepare_trace_runtime()
        package_name = self._resolve_wrap_package(packages)
        metadata = self._trace_metadata(mode="wrap")
        base_payload = {
            **metadata,
            "trace_mode": "wrap",
            "action_json_path": action_json_path,
            "packages": _unique_keep_order(([package_name] if package_name else []) + list(packages or [])),
            "missing_packages": [],
            "start_failures": [],
            "targets": [],
            "local_trace_dir": local_trace_dir,
            "remote_trace_dir": "",
            "captured_at": int(time.time()),
            "trace_name": "step_%03d_%s" % (_json_step_index(action_json_path), _sanitize_name(action_name)),
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

    def _stop_trace_attach(self, pending):
        trace_files = []
        targets = pending.get("targets", [])
        if targets:
            if self.trace_stop_delay > 0:
                time.sleep(self.trace_stop_delay)
            for target in targets:
                tracer_pid = target.get("tracer_pid", "")
                if tracer_pid:
                    self._run_device_shell("kill -INT %s" % tracer_pid, require_root=True)
            for target in targets:
                remote_prefix = target["remote_prefix"]
                if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                    remote_files = self._list_remote_trace_files_run_as(target["run_as_package"], remote_prefix)
                else:
                    remote_files = self._list_remote_trace_files(remote_prefix)
                for remote_file in remote_files:
                    if target.get("trace_launch_mode") == "run_as" and target.get("run_as_package"):
                        local_file, pull_result = self._pull_run_as_file(target["run_as_package"], remote_file, pending["local_trace_dir"])
                    else:
                        local_file = os.path.abspath(os.path.join(pending["local_trace_dir"], os.path.basename(remote_file)))
                        pull_result = self._run_host_cmd(self._adb_base_cmd() + ["pull", remote_file, pending["local_trace_dir"]], timeout_sec=max(self._trace_cmd_timeout(), 30))
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

        return {
            **self._trace_metadata(mode="attach"),
            "packages": pending.get("packages", []),
            "missing_packages": pending.get("missing_packages", []),
            "start_failures": pending.get("start_failures", []),
            "targets": targets,
            "trace_files": sorted(trace_files),
            "trace_file_count": len(trace_files),
            "local_trace_dir": pending.get("local_trace_dir", ""),
            "remote_trace_dir": pending.get("remote_trace_dir", ""),
            "captured_at": int(time.time()),
        }

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

    def _stop_trace_wrap(self, pending):
        if self.trace_stop_delay > 0:
            time.sleep(self.trace_stop_delay)

        runtime = self._wrap_runtime
        metadata = self._trace_metadata(mode="wrap")
        if not runtime or not runtime.get("ready"):
            return {
                **metadata,
                "packages": pending.get("packages", []),
                "missing_packages": pending.get("missing_packages", []),
                "start_failures": pending.get("start_failures", []),
                "targets": pending.get("targets", []),
                "trace_files": [],
                "trace_file_count": 0,
                "local_trace_dir": pending.get("local_trace_dir", ""),
                "remote_trace_dir": pending.get("remote_trace_dir", ""),
                "captured_at": int(time.time()),
            }

        local_trace_dir = pending.get("local_trace_dir", "")
        os.makedirs(local_trace_dir, exist_ok=True)
        remote_trace_files = self._list_remote_trace_files_run_as(runtime["package"], runtime["remote_trace_prefix"])
        baseline_offsets = pending.get("baseline_offsets", {})
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
                os.path.join(local_trace_dir, "%s__%s" % (_sanitize_name(pending.get("trace_name", "trace")), os.path.basename(remote_file)))
            )
            if self._extract_file_delta(local_snapshot_path, start_offset, local_delta_path):
                trace_files.append(local_delta_path)
            try:
                os.remove(local_snapshot_path)
            except OSError:
                pass

        return {
            **metadata,
            "packages": pending.get("packages", []),
            "missing_packages": pending.get("missing_packages", []),
            "start_failures": pending.get("start_failures", []),
            "targets": [target_info],
            "trace_files": sorted(trace_files),
            "trace_file_count": len(trace_files),
            "local_trace_dir": local_trace_dir,
            "remote_trace_dir": runtime["remote_trace_dir"],
            "captured_at": int(time.time()),
        }

    def _attach_execution_trace(self, action_json_path, trace_info):
        with open(action_json_path, "r", encoding="utf-8") as infile:
            payload = json.load(infile)

        payload["execution_trace"] = trace_info
        payload.setdefault("environment_snapshot", {})
        payload["environment_snapshot"]["execution_trace"] = trace_info

        with open(action_json_path, "w", encoding="utf-8") as outfile:
            json.dump(payload, outfile, indent=2, ensure_ascii=False)

    def _run_external_guard(self, action_json_path):
        cmd = [
            self.python_exec,
            self.script_path,
            "--task",
            self.instruction,
            "--action-file",
            action_json_path,
            "--device",
            self.device,
        ]
        if _is_router_script(self.script_path):
            cmd.extend(["--method", self.guard_method])

        if self.adb_path:
            cmd.extend(["--adb-path", self.adb_path])
        if _is_router_script(self.script_path):
            api_base = (
                os.getenv("MOBILEGPT_GUARD_API_BASE", "")
                or os.getenv("GUARD_API_BASE", "")
                or os.getenv("OPENAI_API_BASE", "")
                or os.getenv("OPENAI_BASE_URL", "")
                or os.getenv("API_BASE", "")
            )
            api_key = (
                os.getenv("MOBILEGPT_GUARD_API_KEY", "")
                or os.getenv("GUARD_API_KEY", "")
                or os.getenv("OPENAI_API_KEY", "")
                or os.getenv("CLAUDE_API_KEY", "")
                or os.getenv("APIKey", "")
            )
            text_model = os.getenv("TEXT_MODEL", "") or os.getenv("OPENAI_MODEL_NAME", "")
            vision_model = os.getenv("VISION_MODEL", "") or os.getenv("OPENAI_VISION_MODEL", "") or text_model
            if api_base:
                cmd.extend(["--api-base", api_base])
            if api_key:
                cmd.extend(["--api-key", api_key])
            if text_model:
                cmd.extend(["--model", text_model])
            if vision_model:
                cmd.extend(["--vision-model", vision_model])
        if self.allowed_package:
            cmd.extend(["--allowed-package", self.allowed_package])
        if self.allowed_activity_prefix:
            cmd.extend(["--allowed-activity-prefix", self.allowed_activity_prefix])
        if self.include_screenshot:
            cmd.append("--include-screenshot")

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        parsed_stdout = None
        if result.stdout:
            try:
                parsed_stdout = json.loads(result.stdout)
            except json.JSONDecodeError:
                parsed_stdout = None
        result_root = os.path.join(self.log_directory or ".", self.result_dir_name, self.guard_method or "guard")
        os.makedirs(result_root, exist_ok=True)
        screen_index = _json_step_index(action_json_path)
        result_path = os.path.join(result_root, "step_%03d_guard_output.json" % int(screen_index))
        with open(result_path, "w", encoding="utf-8") as outfile:
            json.dump({
                "command": cmd,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "parsed_stdout": parsed_stdout,
            }, outfile, indent=2, ensure_ascii=False)
        return result_path
