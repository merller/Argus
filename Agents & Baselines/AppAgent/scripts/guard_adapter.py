import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from and_controller import execute_adb
from utils import print_with_color


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


def _sanitize_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "guard")


def _normalize_guard_method(value):
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "agent_sight": "agentsight",
        "agentsight": "agentsight",
        "agent_sentinel": "agent_sentinel",
        "agentsentinel": "agent_sentinel",
        "sentinel": "agent_sentinel",
        "taskshield": "task_shield",
        "delimiter": "delimiters",
        "temporal": "temporal_pusv",
        "pusv": "temporal_pusv",
    }
    return aliases.get(key, key)


def _detect_guard_method(script_path, configured_name=""):
    if configured_name:
        return _sanitize_name(_normalize_guard_method(configured_name))

    lowered = (script_path or "").lower()
    if "agrail" in lowered:
        return "agrail"
    if "sentinel" in lowered:
        return "agent_sentinel"
    if "agentsight" in lowered or "agent_sight" in lowered:
        return "agentsight"
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


def get_guard_method_name_from_configs(configs):
    return _detect_guard_method(
        configs.get("GUARD_SCRIPT_PATH", ""),
        configs.get("GUARD_METHOD_NAME", ""),
    )


def get_guard_script_path_from_configs(configs):
    configured = str(configs.get("GUARD_SCRIPT_PATH", "") or "").strip()
    if configured:
        return configured
    guard_method = get_guard_method_name_from_configs(configs)
    if guard_method in ROUTER_METHODS:
        return ROUTER_SCRIPT_PATH
    return ""


def _is_router_script(script_path):
    return os.path.basename(script_path or "").lower() == "android_guard_router.py"


def _json_safe(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def extract_response_fields(response_text):
    patterns = {
        "observation": r"Observation: (.*?)$",
        "thought": r"Thought: (.*?)$",
        "action": r"Action: (.*?)$",
        "summary": r"Summary: (.*?)$",
    }
    fields = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, response_text, re.MULTILINE)
        fields[key] = matches[0] if matches else ""
    return fields


def get_current_package_and_activity(device):
    package_name = ""
    activity_name = ""

    window_dump = execute_adb(f"adb -s {device} shell dumpsys window windows")
    if window_dump != "ERROR":
        match = re.search(r"mCurrentFocus=.*? ([A-Za-z0-9._]+)/([A-Za-z0-9_.$]+)\}", window_dump)
        if match:
            package_name = match.group(1)
            activity_name = f"{match.group(1)}/{match.group(2)}"

    if not package_name:
        activity_dump = execute_adb(f"adb -s {device} shell dumpsys activity activities")
        if activity_dump != "ERROR":
            match = re.search(r"mResumedActivity:.*? ([A-Za-z0-9._]+)/([A-Za-z0-9_.$]+) ", activity_dump)
            if not match:
                match = re.search(r"topResumedActivity=.*? ([A-Za-z0-9._]+)/([A-Za-z0-9_.$]+) ", activity_dump)
            if match:
                package_name = match.group(1)
                activity_name = f"{match.group(1)}/{match.group(2)}"

    return package_name, activity_name


def get_xml_root_package(xml_path):
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return ""
    for elem in root.iter():
        package_name = elem.attrib.get("package", "")
        if package_name:
            return package_name
    return ""


def get_element_text(elem):
    if elem is None:
        return ""
    raw_attrib = getattr(elem, "raw_attrib", {}) or {}
    for key in ["text", "content-desc", "resource-id", "class"]:
        value = raw_attrib.get(key, "")
        if value:
            return value
    return getattr(elem, "uid", "")


def build_action_payload(act_name, act_params, elem_list, current_package, current_activity):
    payload = {
        "tool": act_name,
        "args": {},
        "target_text": "",
        "target_package": current_package,
        "target_activity": current_activity,
        "target_uid": "",
        "target_bbox": None,
    }

    if act_name in ["tap", "long_press", "swipe"] and elem_list:
        area = act_params[1]
        target_elem = elem_list[area - 1]
        payload["target_text"] = get_element_text(target_elem)
        payload["target_uid"] = target_elem.uid
        payload["target_bbox"] = target_elem.bbox
        payload["args"]["area"] = area
        if act_name == "swipe":
            payload["args"]["direction"] = act_params[2]
            payload["args"]["dist"] = act_params[3]
    elif act_name == "text":
        payload["args"]["text"] = act_params[1]
        payload["target_text"] = act_params[1]
    elif act_name in ["tap_grid", "long_press_grid"]:
        payload["args"]["area"] = act_params[1]
        payload["args"]["subarea"] = act_params[2]
        payload["target_uid"] = f"grid:{act_params[1]}:{act_params[2]}"
    elif act_name == "swipe_grid":
        payload["args"]["start_area"] = act_params[1]
        payload["args"]["start_subarea"] = act_params[2]
        payload["args"]["end_area"] = act_params[3]
        payload["args"]["end_subarea"] = act_params[4]
        payload["target_uid"] = f"grid:{act_params[1]}:{act_params[2]}->{act_params[3]}:{act_params[4]}"

    return payload


def export_guard_input(configs, device, app, task_desc, round_count, screenshot_path, xml_path, response_text,
                       act_name, act_params, elem_list, task_dir):
    if not configs.get("GUARD_EXPORT_ENABLED", False):
        return None

    guard_method = get_guard_method_name_from_configs(configs)

    current_package, current_activity = get_current_package_and_activity(device)
    if not current_package:
        current_package = get_xml_root_package(xml_path)

    response_fields = extract_response_fields(response_text)
    action_payload = build_action_payload(act_name, act_params, elem_list, current_package, current_activity)
    action_payload["observation"] = response_fields["observation"]
    action_payload["thought"] = response_fields["thought"]
    action_payload["summary"] = response_fields["summary"]
    action_payload["raw_action"] = response_fields["action"]

    export_dir_name = configs.get("GUARD_EXPORT_DIR", "guard_inputs")
    export_dir = os.path.join(task_dir, export_dir_name, guard_method)
    os.makedirs(export_dir, exist_ok=True)

    payload = {
        "timestamp": int(time.time()),
        "app": app,
        "step": round_count,
        "guard_method": guard_method,
        "device": device,
        "task": task_desc,
        "user_request": task_desc,
        "screenshot_path": os.path.abspath(screenshot_path),
        "xml_path": os.path.abspath(xml_path),
        "current_package": current_package,
        "current_activity": current_activity,
        "agent_action": action_payload,
        "environment_snapshot": {
            "screenshot_path": os.path.abspath(screenshot_path),
            "xml_path": os.path.abspath(xml_path),
            "current_package": current_package,
            "current_activity": current_activity,
        },
    }

    json_path = os.path.join(export_dir, f"step_{round_count:03d}_action.json")
    with open(json_path, "w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)

    print_with_color(f"[{guard_method}] Guard input exported to {json_path}", "cyan")
    return {"json_path": json_path, "payload": payload}


def attach_execution_trace(action_json_path, trace_info):
    if not action_json_path or not trace_info:
        return None

    with open(action_json_path, "r", encoding="utf-8") as infile:
        payload = json.load(infile)

    safe_trace_info = _json_safe(trace_info)
    payload["execution_trace"] = safe_trace_info
    payload.setdefault("environment_snapshot", {})
    payload["environment_snapshot"]["execution_trace"] = safe_trace_info

    with open(action_json_path, "w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)

    print_with_color(f"Execution trace attached to {action_json_path}", "cyan")
    return action_json_path


def run_external_guard(configs, device, task_desc, action_json_path, task_dir, round_count):
    if not configs.get("GUARD_RUN_EXTERNAL", False):
        return None

    guard_method = get_guard_method_name_from_configs(configs)
    script_path = get_guard_script_path_from_configs(configs)
    if not script_path:
        print_with_color("GUARD_RUN_EXTERNAL is enabled but GUARD_SCRIPT_PATH is empty.", "red")
        return None

    python_exec = configs.get("GUARD_PYTHON_EXEC", "python")
    cmd = [python_exec, script_path, "--task", task_desc, "--action-file", action_json_path, "--device", device]
    if _is_router_script(script_path):
        cmd.extend(["--method", guard_method])

    adb_path = configs.get("ADB_PATH", "adb")
    if adb_path:
        cmd.extend(["--adb-path", adb_path])

    if _is_router_script(script_path):
        api_base = configs.get("OPENAI_API_BASE", "") or configs.get("OPENAI_BASE_URL", "")
        api_key = configs.get("OPENAI_API_KEY", "") or configs.get("APIKey", "")
        text_model = configs.get("TEXT_MODEL", "") or configs.get("OPENAI_MODEL_NAME", "") or configs.get("OPENAI_API_MODEL", "")
        vision_model = configs.get("VISION_MODEL", "") or configs.get("OPENAI_VISION_MODEL", "") or text_model
        if api_base:
            cmd.extend(["--api-base", str(api_base)])
        if api_key:
            cmd.extend(["--api-key", str(api_key)])
        if text_model:
            cmd.extend(["--model", str(text_model)])
        if vision_model:
            cmd.extend(["--vision-model", str(vision_model)])

    allowed_package = configs.get("GUARD_ALLOWED_PACKAGE", "")
    if allowed_package:
        cmd.extend(["--allowed-package", allowed_package])

    allowed_activity_prefix = configs.get("GUARD_ALLOWED_ACTIVITY_PREFIX", "")
    if allowed_activity_prefix:
        cmd.extend(["--allowed-activity-prefix", allowed_activity_prefix])

    if configs.get("GUARD_INCLUDE_SCREENSHOT", True):
        cmd.append("--include-screenshot")

    result_dir_name = configs.get("GUARD_RESULT_DIR", "guard_results")
    result_dir = os.path.join(task_dir, result_dir_name, guard_method)
    os.makedirs(result_dir, exist_ok=True)

    print_with_color(f"Running external Android guard for evaluation [{guard_method}]...", "cyan")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    parsed_stdout = None
    if result.stdout:
        try:
            parsed_stdout = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None

    result_path = os.path.join(result_dir, f"step_{round_count:03d}_guard_output.json")
    with open(result_path, "w", encoding="utf-8") as outfile:
        json.dump({
            "guard_method": guard_method,
            "command": cmd,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "parsed_stdout": parsed_stdout,
        }, outfile, indent=2, ensure_ascii=False)

    if result.returncode == 0:
        print_with_color(f"[{guard_method}] External guard output saved to {result_path}", "cyan")
    else:
        print_with_color(
            f"[{guard_method}] External guard exited with code {result.returncode}. Output saved to {result_path}",
            "red",
        )
    return result_path
