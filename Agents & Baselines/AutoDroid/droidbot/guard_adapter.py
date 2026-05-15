import ast
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from .input_event import KeyEvent, SetTextEvent, TouchEvent, LongTouchEvent, ScrollEvent, SwipeEvent, IntentEvent


ROUTER_SCRIPT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir, "android_guard_router.py")
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


def _get_first_env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_flag_any(*names, default=False):
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        return value.lower() in ["1", "true", "yes", "y", "on"]
    return default


def _env_flag_explicit(*names):
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        return value.lower() in ["1", "true", "yes", "y", "on"]
    return None


def _has_any_env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return True
    return False


def _ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


def _sanitize_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value or "unknown")


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


def _detect_guard_method(script_path="", configured_name=""):
    if configured_name:
        return _sanitize_name(_normalize_guard_method(configured_name))

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


def get_guard_method_name():
    configured_name = _get_first_env("AUTODROID_GUARD_METHOD_NAME", "GUARD_METHOD_NAME", "DEFENSE_METHOD", default="")
    if configured_name:
        return _sanitize_name(configured_name)

    # When defenses are disabled we still export AppAgent-style artifacts, and
    # the reference folder structure always uses guard_inputs/guard.
    if not is_external_guard_enabled():
        return "guard"

    configured_script = _get_first_env(
        "AUTODROID_GUARD_SCRIPT_PATH",
        "GUARD_SCRIPT_PATH",
        "SENTINEL_WRAP",
        "AGRAIL_WRAP",
        "SENTINEL",
        "AGRAIL",
        default="",
    )
    return _detect_guard_method(configured_script, "")


def get_guard_script_path():
    configured_script = _get_first_env("AUTODROID_GUARD_SCRIPT_PATH", "GUARD_SCRIPT_PATH", "DEFENSE_SCRIPT", default="")
    if configured_script:
        return configured_script

    guard_method = get_guard_method_name()
    if guard_method in ROUTER_METHODS:
        return ROUTER_SCRIPT_PATH
    if guard_method == "agrail":
        return _get_first_env("AGRAIL_WRAP", "AGRAIL", "SENTINEL_WRAP", "SENTINEL", default="")
    if guard_method == "agent_sentinel":
        return _get_first_env("SENTINEL_WRAP", "SENTINEL", "AGRAIL_WRAP", "AGRAIL", default="")
    return _get_first_env("SENTINEL_WRAP", "SENTINEL", "AGRAIL_WRAP", "AGRAIL", default="")


def _is_router_script(script_path):
    return os.path.basename(script_path or "").lower() == "android_guard_router.py"


def get_guard_python_exec():
    return _get_first_env("AUTODROID_GUARD_PYTHON_EXEC", "GUARD_PY", "AUTO_PY", default="python")


def get_guard_device():
    return _get_first_env("AUTODROID_GUARD_DEVICE", "GUARD_DEVICE", "SERIAL", default="")


def get_guard_adb_path():
    return _get_first_env("AUTODROID_GUARD_ADB_PATH", "ADB_PATH", "ADB", default="")


def get_guard_allowed_package():
    return _get_first_env("AUTODROID_GUARD_ALLOWED_PACKAGE", "GUARD_ALLOWED_PACKAGE", "PKG", default="")


def get_guard_allowed_activity_prefix():
    return _get_first_env(
        "AUTODROID_GUARD_ALLOWED_ACTIVITY_PREFIX",
        "GUARD_ALLOWED_ACTIVITY_PREFIX",
        "ACT_PREFIX",
        default="",
    )


def get_guard_export_dir_name():
    return _get_first_env("AUTODROID_GUARD_EXPORT_DIR", "GUARD_EXPORT_DIR", default="guard_inputs")


def get_guard_result_dir_name():
    return _get_first_env("AUTODROID_GUARD_RESULT_DIR", "GUARD_RESULT_DIR", default="guard_results")


def get_task_export_root_name():
    return _get_first_env("AUTODROID_TASK_EXPORT_ROOT", "TASK_EXPORT_ROOT", default="task")


def get_task_export_agent_name():
    return _get_first_env("AUTODROID_TASK_EXPORT_AGENT_NAME", "TASK_EXPORT_AGENT_NAME", default="AutoDroid")


def get_task_steps_dir_name():
    return _get_first_env("AUTODROID_TASK_EXPORT_STEPS_DIR", "TASK_EXPORT_STEPS_DIR", default="steps")


def get_task_export_dir(output_dir):
    return os.path.join(output_dir or ".", get_task_export_root_name(), get_task_export_agent_name())


def get_task_steps_dir(output_dir):
    return os.path.join(get_task_export_dir(output_dir), get_task_steps_dir_name())


def get_task_yaml_path(output_dir, task_name):
    safe_name = _sanitize_name(task_name or "task")
    if not safe_name:
        safe_name = "task"
    return os.path.join(get_task_export_dir(output_dir), "%s.yaml" % safe_name)


def get_task_record_path(output_dir):
    return os.path.join(get_task_export_dir(output_dir), "%s.txt" % get_task_export_agent_name())


def get_task_guard_inputs_dir(output_dir, guard_method=None):
    resolved_guard_method = guard_method or get_guard_method_name()
    return os.path.join(
        get_task_export_dir(output_dir),
        get_guard_export_dir_name(),
        resolved_guard_method,
    )


def get_trace_dir_name():
    return _get_first_env(
        "AUTODROID_GUARD_TRACE_DIR_NAME",
        "GUARD_TRACE_DIR_NAME",
        "DEVICE_TRACE_DIR_NAME",
        default="syscall_traces",
    )


def is_trace_enabled():
    explicit = _env_flag_explicit(
        "AUTODROID_GUARD_TRACE_ENABLED",
        "GUARD_TRACE_ENABLED",
        "DEVICE_TRACE_ENABLED",
    )
    if explicit is not None:
        return explicit
    return False


def is_guard_export_enabled():
    explicit = _env_flag_explicit(
        "AUTODROID_GUARD_EXPORT_ENABLED",
        "GUARD_EXPORT_ENABLED",
        "AUTODROID_TASK_EXPORT_ENABLED",
    )
    if explicit is not None:
        return explicit
    return True


def is_external_guard_enabled():
    explicit = _env_flag_explicit("AUTODROID_GUARD_RUN_EXTERNAL", "GUARD_RUN_EXTERNAL")
    if explicit is not None:
        return explicit
    return False


def should_run_guard_after_action(trace_enabled=False):
    explicit = os.environ.get("AUTODROID_GUARD_RUN_AFTER_ACTION")
    if explicit is None:
        explicit = os.environ.get("GUARD_RUN_AFTER_ACTION")
    if explicit is not None:
        return explicit.lower() in ["1", "true", "yes", "y", "on"]
    return trace_enabled


def _view_target_text(view):
    if not view:
        return ""
    for key in ["text", "content_description", "resource_id", "class"]:
        value = view.get(key)
        if value:
            return str(value)
    return view.get("view_str", "")


def _view_bounds(view):
    if not view:
        return None
    return view.get("bounds")


def _json_safe(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _safe_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), ensure_ascii=False)
    return str(value)


def _bool_text(value):
    return "true" if bool(value) else "false"


def _bounds_text(bounds):
    if not bounds or len(bounds) != 2:
        return "[0,0][0,0]"
    return "[%d,%d][%d,%d]" % (
        int(bounds[0][0]),
        int(bounds[0][1]),
        int(bounds[1][0]),
        int(bounds[1][1]),
    )


def _unwrap_touch_action(candidate_action):
    if isinstance(candidate_action, list):
        filtered_actions = [item for item in candidate_action if item is not None]
        candidate_action = filtered_actions[-1] if filtered_actions else None
    if not isinstance(candidate_action, TouchEvent):
        return None
    if not getattr(candidate_action, "view", None):
        return None
    if not _view_bounds(candidate_action.view):
        return None
    return candidate_action


def _build_click_candidates_from_actions(candidate_actions, dedupe=False):
    click_candidates = []
    seen_keys = set()

    for prompt_index, candidate_action in enumerate(candidate_actions or []):
        touch_action = _unwrap_touch_action(candidate_action)
        if touch_action is None:
            continue

        view = touch_action.view
        target_uid = view.get("view_str", "") if isinstance(view, dict) else ""
        target_bbox = _view_bounds(view)
        dedupe_key = target_uid or json.dumps(_json_safe(target_bbox), ensure_ascii=False)
        if dedupe and dedupe_key in seen_keys:
            continue

        click_candidates.append(
            {
                "area": len(click_candidates) + 1,
                "prompt_index": prompt_index,
                "view": view,
                "target_uid": target_uid,
                "target_bbox": target_bbox,
            }
        )
        if dedupe:
            seen_keys.add(dedupe_key)
    return click_candidates


def _current_click_candidates(current_state, candidate_actions=None):
    if candidate_actions is not None:
        return _build_click_candidates_from_actions(candidate_actions, dedupe=False)

    if current_state is None or not getattr(current_state, "get_described_actions", None):
        return []

    try:
        _, candidate_actions, _, _ = current_state.get_described_actions()
    except Exception:
        return []

    return _build_click_candidates_from_actions(candidate_actions, dedupe=True)


def _match_click_candidate(action, click_candidates):
    if not isinstance(action, TouchEvent) or not action.view:
        return None

    action_uid = action.view.get("view_str", "")
    action_bounds = _view_bounds(action.view)
    for candidate in click_candidates:
        if action_uid and candidate.get("target_uid") == action_uid:
            return candidate
        if action_bounds and candidate.get("target_bbox") == action_bounds:
            return candidate
    return None


def _find_click_candidate_by_prompt_index(prompt_index, click_candidates):
    if prompt_index is None:
        return None
    for candidate in click_candidates:
        if candidate.get("prompt_index") == prompt_index:
            return candidate
    return None


def _next_click_step_index(guard_input_dir):
    if not os.path.isdir(guard_input_dir):
        return 1

    max_step = 0
    for file_name in os.listdir(guard_input_dir):
        match = re.match(r"step_(\d+)_action\.json$", file_name)
        if match:
            max_step = max(max_step, int(match.group(1)))
    return max_step + 1


def _copy_screenshot_as_png(source_path, dest_path):
    from PIL import Image

    if not source_path or not os.path.exists(source_path):
        return ""

    with Image.open(source_path) as image:
        image.convert("RGBA").save(dest_path, format="PNG")
    return dest_path


def _draw_labeled_screenshot(source_path, dest_path, click_candidates):
    from PIL import Image, ImageDraw, ImageFont

    if not source_path or not os.path.exists(source_path):
        return ""

    with Image.open(source_path) as base_image:
        image = base_image.convert("RGBA")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        for candidate in click_candidates:
            bounds = candidate.get("target_bbox")
            if not bounds:
                continue
            center_x = int((bounds[0][0] + bounds[1][0]) / 2)
            center_y = int((bounds[0][1] + bounds[1][1]) / 2)
            label = str(candidate.get("area", ""))
            radius = 22
            left = max(0, center_x - radius)
            top = max(0, center_y - radius)
            right = min(image.width, center_x + radius)
            bottom = min(image.height, center_y + radius)
            draw.ellipse((left, top, right, bottom), fill=(220, 38, 38, 230), outline=(255, 255, 255, 255), width=2)
            try:
                text_box = draw.textbbox((0, 0), label, font=font)
                text_width = text_box[2] - text_box[0]
                text_height = text_box[3] - text_box[1]
            except AttributeError:
                text_width, text_height = draw.textsize(label, font=font)
            draw.text(
                (center_x - text_width / 2, center_y - text_height / 2),
                label,
                fill=(255, 255, 255, 255),
                font=font,
            )

        image.save(dest_path, format="PNG")
    return dest_path


def _append_record_line(txt_path, payload):
    parent_dir = os.path.dirname(txt_path)
    if parent_dir:
        _ensure_dir(parent_dir)

    encoded = json.dumps(payload, ensure_ascii=False)
    needs_newline = os.path.exists(txt_path) and os.path.getsize(txt_path) > 0
    with open(txt_path, "a", encoding="utf-8") as outfile:
        if needs_newline:
            outfile.write("\n")
        outfile.write(encoded)


def _cleanup_legacy_task_export(task_export_dir):
    legacy_steps_dir = os.path.join(task_export_dir, "steps")
    if os.path.isdir(legacy_steps_dir):
        shutil.rmtree(legacy_steps_dir, ignore_errors=True)

    for file_name in os.listdir(task_export_dir):
        file_path = os.path.join(task_export_dir, file_name)
        if os.path.isfile(file_path) and file_name.lower().endswith(".yaml"):
            try:
                os.remove(file_path)
            except OSError:
                pass


def _view_to_xml_attrs(view, index):
    resource_id = view.get("resource_id", "") if isinstance(view, dict) else ""
    content_description = view.get("content_description", "") if isinstance(view, dict) else ""
    return {
        "index": str(index),
        "text": _safe_text(view.get("text", "") if isinstance(view, dict) else ""),
        "resource-id": _safe_text(resource_id or ""),
        "class": _safe_text(view.get("class", "") if isinstance(view, dict) else ""),
        "package": _safe_text(view.get("package", "") if isinstance(view, dict) else ""),
        "content-desc": _safe_text(content_description or ""),
        "checkable": _bool_text(view.get("checkable", False) if isinstance(view, dict) else False),
        "checked": _bool_text(view.get("checked", False) if isinstance(view, dict) else False),
        "clickable": _bool_text(view.get("clickable", False) if isinstance(view, dict) else False),
        "enabled": _bool_text(view.get("enabled", True) if isinstance(view, dict) else True),
        "focusable": _bool_text(view.get("focusable", False) if isinstance(view, dict) else False),
        "focused": _bool_text(view.get("focused", False) if isinstance(view, dict) else False),
        "scrollable": _bool_text(view.get("scrollable", False) if isinstance(view, dict) else False),
        "long-clickable": _bool_text(view.get("long_clickable", False) if isinstance(view, dict) else False),
        "password": _bool_text(
            (view.get("is_password", False) or view.get("password", False)) if isinstance(view, dict) else False
        ),
        "selected": _bool_text(view.get("selected", False) if isinstance(view, dict) else False),
        "bounds": _bounds_text(view.get("bounds") if isinstance(view, dict) else None),
    }


def _append_xml_children(parent_xml_node, children):
    if not isinstance(children, list):
        return
    for child_index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        child_xml = ET.SubElement(parent_xml_node, "node", _view_to_xml_attrs(child, child_index))
        _append_xml_children(child_xml, child.get("children", []))


def _write_state_xml(current_state, dest_path):
    hierarchy = ET.Element("hierarchy", {"rotation": "0"})
    view_tree = getattr(current_state, "view_tree", {}) or {}
    if isinstance(view_tree, dict) and view_tree:
        root_node = ET.SubElement(hierarchy, "node", _view_to_xml_attrs(view_tree, 0))
        _append_xml_children(root_node, view_tree.get("children", []))
    tree = ET.ElementTree(hierarchy)
    tree.write(dest_path, encoding="utf-8", xml_declaration=True)
    return dest_path


def _parse_response_fields(response_text):
    if not response_text:
        return {}
    if isinstance(response_text, dict):
        return response_text
    for parser in (json.loads, ast.literal_eval):
        try:
            payload = parser(response_text)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _serialize_action(action, observation_text="", response_text=None, current_package="", current_activity=""):
    payload = {
        "tool": "unknown",
        "args": {},
        "target_text": "",
        "target_package": current_package,
        "target_activity": current_activity,
        "target_uid": "",
        "target_bbox": None,
    }

    if isinstance(action, TouchEvent):
        payload["tool"] = "tap"
        payload["target_text"] = _view_target_text(action.view)
        payload["target_uid"] = action.view.get("view_str", "") if action.view else ""
        payload["target_bbox"] = _view_bounds(action.view)
    elif isinstance(action, LongTouchEvent):
        payload["tool"] = "long_press"
        payload["target_text"] = _view_target_text(action.view)
        payload["target_uid"] = action.view.get("view_str", "") if action.view else ""
        payload["target_bbox"] = _view_bounds(action.view)
    elif isinstance(action, SetTextEvent):
        payload["tool"] = "input"
        payload["args"]["text"] = action.text
        payload["target_text"] = _view_target_text(action.view)
        payload["target_uid"] = action.view.get("view_str", "") if action.view else ""
        payload["target_bbox"] = _view_bounds(action.view)
    elif isinstance(action, ScrollEvent):
        payload["tool"] = "scroll"
        payload["args"]["direction"] = action.direction
        payload["target_text"] = _view_target_text(action.view)
        payload["target_uid"] = action.view.get("view_str", "") if action.view else ""
        payload["target_bbox"] = _view_bounds(action.view)
    elif isinstance(action, SwipeEvent):
        payload["tool"] = "swipe"
        payload["args"]["start_x"] = action.start_x
        payload["args"]["start_y"] = action.start_y
        payload["args"]["end_x"] = action.end_x
        payload["args"]["end_y"] = action.end_y
        payload["target_text"] = _view_target_text(action.start_view)
        payload["target_uid"] = action.start_view.get("view_str", "") if action.start_view else ""
        payload["target_bbox"] = _view_bounds(action.start_view)
    elif isinstance(action, KeyEvent):
        payload["tool"] = "key"
        payload["args"]["key"] = action.name
        payload["target_text"] = action.name
        payload["target_uid"] = action.name
    elif isinstance(action, IntentEvent):
        payload["tool"] = "intent"
        payload["args"]["intent"] = action.intent
        payload["target_text"] = action.intent
        payload["target_uid"] = action.intent

    response_fields = _parse_response_fields(response_text)
    payload["observation"] = observation_text or _safe_text(
        response_fields.get("Observation")
        or response_fields.get("observation")
        or response_fields.get("Analyses")
        or response_fields.get("analyses")
        or ""
    )
    payload["thought"] = _safe_text(
        response_fields.get("Thought")
        or response_fields.get("thought")
        or response_fields.get("Analyses")
        or response_fields.get("analyses")
        or ""
    )
    payload["summary"] = _safe_text(
        response_fields.get("Summary")
        or response_fields.get("summary")
        or response_fields.get("Next step")
        or response_fields.get("next step")
        or response_fields.get("Steps")
        or response_fields.get("steps")
        or ""
    )
    payload["raw_action"] = _safe_text(
        response_fields.get("Action")
        or response_fields.get("action")
        or response_fields.get("raw_action")
        or response_text
        or ""
    )
    payload["raw_response"] = _safe_text(response_text)

    return payload


def export_guard_input(device, app, task, current_state, prompt, llm_response, action, action_history, thought_history,
                       action_sequence=None, selected_index=None, candidate_actions=None):
    if not is_guard_export_enabled():
        return None
    if current_state is None:
        return None
    if not isinstance(action, TouchEvent):
        return None

    output_dir = device.output_dir or "."
    guard_method = get_guard_method_name()
    task_export_dir = get_task_export_dir(output_dir)
    export_dir = get_task_guard_inputs_dir(output_dir, guard_method=guard_method)
    _ensure_dir(task_export_dir)
    _cleanup_legacy_task_export(task_export_dir)
    _ensure_dir(export_dir)

    current_package = ""
    current_activity = ""
    if current_state and getattr(current_state, "foreground_activity", ""):
        current_activity = current_state.foreground_activity
        current_package = current_activity.split("/", 1)[0] if "/" in current_activity else current_activity
    if not current_package and app is not None and getattr(app, "get_package_name", None):
        current_package = app.get_package_name() or ""

    state_prompt = ""
    if current_state is not None and getattr(current_state, "get_described_actions", None):
        try:
            state_prompt, _, _, _ = current_state.get_described_actions()
        except Exception:
            state_prompt = ""

    click_candidates = _current_click_candidates(current_state, candidate_actions=candidate_actions)
    selected_candidate = _find_click_candidate_by_prompt_index(selected_index, click_candidates)
    if selected_candidate is None:
        selected_candidate = _match_click_candidate(action, click_candidates)
    if selected_candidate is None and isinstance(action, TouchEvent) and getattr(action, "view", None):
        selected_candidate = {
            "area": len(click_candidates) + 1,
            "prompt_index": selected_index,
            "view": action.view,
            "target_uid": action.view.get("view_str", "") if action.view else "",
            "target_bbox": _view_bounds(action.view),
        }
        click_candidates.append(selected_candidate)

    step_index = _next_click_step_index(export_dir)
    asset_stem = "%s_%d" % (get_task_export_agent_name(), step_index)

    screenshot_source = current_state.screenshot_path if current_state and current_state.screenshot_path else ""
    screenshot_path = os.path.join(task_export_dir, "%s.png" % asset_stem)
    labeled_screenshot_path = os.path.join(task_export_dir, "%s_labeled.png" % asset_stem)
    xml_path = os.path.join(task_export_dir, "%s.xml" % asset_stem)

    exported_screenshot = _copy_screenshot_as_png(screenshot_source, screenshot_path)
    exported_xml = _write_state_xml(current_state, xml_path)
    exported_labeled_screenshot = _draw_labeled_screenshot(screenshot_source, labeled_screenshot_path, click_candidates)

    record_payload = {
        "step": step_index,
        "prompt": prompt,
        "image": os.path.basename(exported_labeled_screenshot) if exported_labeled_screenshot else "",
        "response": _safe_text(llm_response),
    }
    _append_record_line(get_task_record_path(output_dir), record_payload)

    action_payload = {
        "tool": "tap",
        "args": {
            "area": selected_candidate["area"],
        },
        "target_text": _view_target_text(action.view),
        "target_package": current_package,
        "target_activity": current_activity,
        "target_uid": action.view.get("view_str", "") if action.view else "",
        "target_bbox": _view_bounds(action.view),
        "observation": "",
        "thought": "",
        "summary": "",
        "raw_action": "",
    }

    payload = {
        "timestamp": int(time.time()),
        "app": app.app_name,
        "step": step_index,
        "guard_method": guard_method,
        "device": device.serial,
        "task": task,
        "user_request": task,
        "screenshot_path": os.path.abspath(exported_screenshot) if exported_screenshot else "",
        "xml_path": os.path.abspath(exported_xml) if exported_xml else "",
        "current_package": current_package,
        "current_activity": current_activity,
        "agent_action": action_payload,
        "environment_snapshot": {
            "screenshot_path": os.path.abspath(exported_screenshot) if exported_screenshot else "",
            "xml_path": os.path.abspath(exported_xml) if exported_xml else "",
            "current_package": current_package,
            "current_activity": current_activity,
        },
    }

    json_path = os.path.join(export_dir, "step_%03d_action.json" % step_index)
    with open(json_path, "w", encoding="utf-8") as outfile:
        json.dump(payload, outfile, indent=2, ensure_ascii=False)
    return {
        "json_path": json_path,
        "payload": payload,
        "step": step_index,
        "guard_method": guard_method,
        "task_output_dir": task_export_dir,
    }


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

    return action_json_path


def run_external_guard(task, action_json_path):
    if not is_external_guard_enabled():
        return None

    script_path = get_guard_script_path()
    if not script_path:
        return None

    python_exec = get_guard_python_exec()
    guard_method = get_guard_method_name()
    cmd = [
        python_exec,
        script_path,
        "--task",
        task,
        "--action-file",
        action_json_path,
    ]
    if _is_router_script(script_path):
        cmd.extend(["--method", guard_method])

    device_name = get_guard_device()
    if device_name:
        cmd.extend(["--device", device_name])

    adb_path = get_guard_adb_path()
    if adb_path:
        cmd.extend(["--adb-path", adb_path])

    if _is_router_script(script_path):
        api_base = _get_first_env("GUARD_API_BASE", "OPENAI_API_BASE", "OPENAI_BASE_URL", "API_BASE", default="")
        api_key = _get_first_env("GUARD_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY", "APIKey", default="")
        text_model = _get_first_env("TEXT_MODEL", "OPENAI_MODEL_NAME", "OPENAI_MODEL", default="")
        vision_model = _get_first_env("VISION_MODEL", "OPENAI_VISION_MODEL", default=text_model)
        if api_base:
            cmd.extend(["--api-base", api_base])
        if api_key:
            cmd.extend(["--api-key", api_key])
        if text_model:
            cmd.extend(["--model", text_model])
        if vision_model:
            cmd.extend(["--vision-model", vision_model])

    allowed_package = get_guard_allowed_package()
    if allowed_package:
        cmd.extend(["--allowed-package", allowed_package])

    allowed_activity_prefix = get_guard_allowed_activity_prefix()
    if allowed_activity_prefix:
        cmd.extend(["--allowed-activity-prefix", allowed_activity_prefix])

    if _env_flag_any("AUTODROID_GUARD_INCLUDE_SCREENSHOT", "GUARD_INCLUDE_SCREENSHOT", default=True):
        cmd.append("--include-screenshot")

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    parsed_stdout = None
    if result.stdout:
        try:
            parsed_stdout = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed_stdout = None

    action_dir = os.path.dirname(os.path.abspath(action_json_path))
    if os.path.basename(os.path.dirname(action_dir)).lower() == get_guard_export_dir_name().lower():
        base_dir = os.path.dirname(os.path.dirname(action_dir))
    else:
        base_dir = os.path.abspath(os.path.join(action_dir, os.pardir, os.pardir))
    result_dir = os.path.join(base_dir, get_guard_result_dir_name(), guard_method)
    _ensure_dir(result_dir)

    result_name = os.path.basename(action_json_path).replace("_action.json", "_guard_output.json")
    result_path = os.path.join(result_dir, result_name)
    result_payload = {
        "guard_method": guard_method,
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "parsed_stdout": parsed_stdout,
    }
    if isinstance(parsed_stdout, dict):
        for key in [
            "decision",
            "unsafe_reasons",
            "safe_reasons",
            "report_path",
            "state_file",
            "trace_files",
            "trace_unavailable_reason",
        ]:
            if key in parsed_stdout:
                result_payload[key] = parsed_stdout[key]
    with open(result_path, "w", encoding="utf-8") as outfile:
        json.dump(result_payload, outfile, indent=2, ensure_ascii=False)
    return result_path
