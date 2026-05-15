import io
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime

try:
    from PIL import Image
except ImportError:
    Image = None

from agents.task_agent import TaskAgent
from guard_adapter import GuardAdapter, _build_interactive_candidates, _match_area_candidate
from mobilegpt import MobileGPT
from screenParser.Encoder import xmlEncoder
from utils.utils import log


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ["1", "true", "yes", "y", "on"]


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_dir(path):
    if path:
        os.makedirs(path, exist_ok=True)


def _parse_bounds(bounds_text):
    matches = re.findall(r"\d+", bounds_text or "")
    if len(matches) != 4:
        return None
    x1, y1, x2, y2 = map(int, matches)
    return (x1, y1), (x2, y2)


def _center_of_bbox(bbox):
    if not bbox:
        return None
    (x1, y1), (x2, y2) = bbox
    return (x1 + x2) // 2, (y1 + y2) // 2


def _renumber_xml_indices(raw_xml):
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return raw_xml

    for index, element in enumerate(root.iter()):
        element.attrib["index"] = str(index)

    return ET.tostring(root, encoding="unicode")


def _extract_current_package(raw_xml):
    match = re.search(r'package="([^"]+)"', raw_xml or "")
    if match:
        return match.group(1)
    return ""


class _BufferedSocket:
    def __init__(self):
        self._buffer = b""
        self._messages = []

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            self._messages.append(line.decode("utf-8", errors="replace"))
        return len(data)

    def pop_messages(self):
        messages = self._messages[:]
        self._messages = []
        return messages


class AdbController:
    def __init__(self, adb_path, serial):
        self.adb_path = adb_path or "adb"
        self.serial = serial

    def _base_cmd(self):
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def run(self, args, timeout_sec=60, binary=False):
        cmd = self._base_cmd() + args
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
        )
        if binary:
            return completed
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        return completed.returncode, stdout, stderr

    def wait_for_device(self):
        return self.run(["wait-for-device"], timeout_sec=60)

    def shell(self, shell_args, timeout_sec=60):
        return self.run(["shell"] + shell_args, timeout_sec=timeout_sec)

    def is_package_installed(self, package_name):
        returncode, stdout, _ = self.shell(["pm", "path", package_name], timeout_sec=30)
        return returncode == 0 and "package:" in stdout

    def install_apk(self, apk_path):
        return self.run(["install", "-r", apk_path], timeout_sec=600)

    def force_stop(self, package_name):
        return self.shell(["am", "force-stop", package_name], timeout_sec=30)

    def start_activity(self, package_name, activity_name=""):
        if activity_name:
            return self.shell(["am", "start", "-W", "-n", activity_name], timeout_sec=60)
        return self.shell(
            ["monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout_sec=60,
        )

    def capture_screenshot(self, dest_path):
        completed = self.run(["exec-out", "screencap", "-p"], timeout_sec=60, binary=True)
        if completed.returncode != 0 or not completed.stdout:
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise RuntimeError("Failed to capture screenshot: %s %s" % (stdout.strip(), stderr.strip()))

        _ensure_dir(os.path.dirname(dest_path))
        png_bytes = completed.stdout
        if Image is None:
            with open(dest_path, "wb") as outfile:
                outfile.write(png_bytes)
            return dest_path

        with Image.open(io.BytesIO(png_bytes)) as image:
            image.convert("RGB").save(dest_path, format="JPEG", quality=95)
        return dest_path

    def dump_ui_xml(self, dest_path):
        remote_path = "/sdcard/mobilegpt_window_dump.xml"
        returncode, stdout, stderr = self.shell(["uiautomator", "dump", remote_path], timeout_sec=60)
        combined = (stdout or "") + "\n" + (stderr or "")
        if returncode != 0 or ("dumped to:" not in combined.lower() and "UI hierchary dumped" not in combined):
            raise RuntimeError("Failed to dump UI hierarchy: %s" % combined.strip())

        completed = self.run(["exec-out", "cat", remote_path], timeout_sec=60, binary=True)
        if completed.returncode != 0 or not completed.stdout:
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise RuntimeError("Failed to read dumped XML: %s %s" % (stdout.strip(), stderr.strip()))

        raw_xml = completed.stdout.decode("utf-8", errors="replace").strip()
        raw_xml = raw_xml.replace("class=\"\"", "class=\"unknown\"")
        raw_xml = _renumber_xml_indices(raw_xml)
        _ensure_dir(os.path.dirname(dest_path))
        with open(dest_path, "w", encoding="utf-8") as outfile:
            outfile.write(raw_xml)
        return raw_xml

    def tap(self, x, y):
        return self.shell(["input", "tap", str(int(x)), str(int(y))], timeout_sec=30)

    def long_press(self, x, y, duration_ms=1000):
        return self.shell(
            ["input", "touchscreen", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(int(duration_ms))],
            timeout_sec=30,
        )

    def swipe(self, start_xy, end_xy, duration_ms=400):
        x1, y1 = start_xy
        x2, y2 = end_xy
        return self.shell(
            ["input", "touchscreen", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms))],
            timeout_sec=30,
        )

    def input_text(self, text):
        if isinstance(text, str):
            escaped = text.replace("%s", "\\%s")
            encoded = escaped.replace(" ", "%s")
        else:
            encoded = str(text)
        return self.shell(["input", "text", encoded], timeout_sec=30)

    def keyevent(self, key_name):
        return self.shell(["input", "keyevent", key_name], timeout_sec=30)


class AdbDirectRunner:
    def __init__(self):
        self.task_instruction = (os.getenv("TASK", "") or "").strip()
        self.adb_path = (os.getenv("ADB", "adb") or "adb").strip()
        self.serial = (os.getenv("SERIAL", "") or "").strip()
        self.apk_path = (os.getenv("APK", "") or "").strip()
        self.package_name = (os.getenv("PKG", "") or "").strip()
        self.activity_name = (os.getenv("ACTIVITY", "") or "").strip()
        self.activity_prefix = (os.getenv("ACT_PREFIX", "") or "").strip()
        self.app_name = (os.getenv("APPAGENT_NAME", "") or "").strip()
        self.max_steps = _safe_int(os.getenv("MOBILEGPT_ADB_MAX_STEPS", "30"), 30)
        self.action_wait = _safe_float(os.getenv("MOBILEGPT_ADB_ACTION_WAIT", "1.5"), 1.5)
        self.initial_wait = _safe_float(os.getenv("MOBILEGPT_ADB_INITIAL_WAIT", "2.0"), 2.0)
        self.force_stop_before_start = _env_flag("MOBILEGPT_ADB_FORCE_STOP_BEFORE_START", True)
        self.install_if_missing = _env_flag("MOBILEGPT_ADB_INSTALL_IF_MISSING", True)
        self.min_dist = _safe_int(os.getenv("MOBILEGPT_GUARD_MIN_DIST", "30"), 30)
        self.qa_answers = self._load_qa_answers()

        self.controller = AdbController(self.adb_path, self.serial)
        self.socket = _BufferedSocket()
        self.mobile_gpt = MobileGPT(self.socket)
        self.guard_adapter = GuardAdapter()
        self.mobile_gpt.guard_adapter = self.guard_adapter
        self.screen_parser = xmlEncoder()
        self.task_agent = TaskAgent()

    @staticmethod
    def _load_qa_answers():
        raw = (os.getenv("MOBILEGPT_QA_JSON", "") or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _resolve_task(self):
        task, is_new_task = self.task_agent.get_task(self.task_instruction)

        if self.app_name:
            task["app"] = self.app_name
        elif not task.get("app"):
            task["app"] = self.package_name or "MobileGPT"

        if not self.package_name:
            raise RuntimeError("PKG must be set for direct ADB mode.")

        return task, is_new_task

    def _ensure_app_started(self):
        self.controller.wait_for_device()

        if self.package_name and not self.controller.is_package_installed(self.package_name):
            if self.apk_path and os.path.exists(self.apk_path) and self.install_if_missing:
                log("Target package is not installed. Installing APK...", "yellow")
                returncode, stdout, stderr = self.controller.install_apk(self.apk_path)
                if returncode != 0:
                    raise RuntimeError("APK installation failed: %s %s" % (stdout.strip(), stderr.strip()))
            else:
                raise RuntimeError("Target package %s is not installed and no usable APK was provided." % self.package_name)

        if self.force_stop_before_start and self.package_name:
            self.controller.force_stop(self.package_name)
            time.sleep(0.5)

        returncode, stdout, stderr = self.controller.start_activity(self.package_name, self.activity_name)
        if returncode != 0:
            raise RuntimeError("Failed to start activity: %s %s" % (stdout.strip(), stderr.strip()))
        time.sleep(self.initial_wait)

    def _prepare_session(self):
        if not self.task_instruction:
            raise RuntimeError("TASK must be set for direct ADB mode.")

        task, is_new_task = self._resolve_task()
        target_app = task["app"]
        target_package = self.package_name

        now = datetime.now()
        dt_string = now.strftime("%Y_%m_%d %H:%M:%S")
        log_directory = "./memory/log/%s/%s/%s/" % (target_app, task["name"], dt_string)
        self.screen_parser.init(log_directory)

        self.mobile_gpt.init(self.task_instruction, task, is_new_task)
        self.mobile_gpt.target_package = target_package
        self.guard_adapter.set_instruction_context(self.task_instruction, task, target_package, log_directory)

        return task, target_package, log_directory

    def _collect_messages(self, returned_action):
        actions = []
        finished = False
        for message in self.socket.pop_messages():
            if not message:
                continue
            if message == "$$$$$":
                finished = True
                continue
            try:
                actions.append(json.loads(message))
            except json.JSONDecodeError:
                log("Ignored non-JSON socket message: %s" % message, "yellow")

        if returned_action is not None:
            actions.append(returned_action)

        return actions, finished

    def _resolve_target_bbox(self, action, raw_xml_path):
        params = (action or {}).get("parameters", {})
        action_index = params.get("index")
        if action_index is None:
            return None

        interactive_candidates, nodes_by_index = _build_interactive_candidates(raw_xml_path, self.min_dist)
        area_candidate, target_node = _match_area_candidate(
            action_index,
            interactive_candidates,
            nodes_by_index,
            self.min_dist,
        )
        resolved = target_node or area_candidate
        if resolved is None:
            return None
        return resolved.get("bbox")

    @staticmethod
    def _resolve_scroll_points(bbox, direction):
        direction = (direction or "down").strip().lower()
        if bbox is None:
            center_x, center_y = 540, 1200
            width = 600
            height = 1000
        else:
            (x1, y1), (x2, y2) = bbox
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2
            width = max(200, x2 - x1)
            height = max(300, y2 - y1)

        dx = max(80, int(width * 0.25))
        dy = max(180, int(height * 0.35))

        if direction == "up":
            return (center_x, center_y + dy), (center_x, center_y - dy)
        if direction == "down":
            return (center_x, center_y - dy), (center_x, center_y + dy)
        if direction == "left":
            return (center_x + dx, center_y), (center_x - dx, center_y)
        if direction == "right":
            return (center_x - dx, center_y), (center_x + dx, center_y)
        return (center_x, center_y + dy), (center_x, center_y - dy)

    def _answer_question(self, action):
        params = (action or {}).get("parameters", {})
        info_name = params.get("info_name", "")
        question = params.get("question", "")

        if info_name in self.qa_answers:
            return self.qa_answers[info_name]
        if question in self.qa_answers:
            return self.qa_answers[question]
        for key, value in self.qa_answers.items():
            if isinstance(key, str) and key and key.lower() in question.lower():
                return value
        return None

    def _execute_action(self, action, raw_xml_path):
        action_name = (action or {}).get("name", "")
        params = (action or {}).get("parameters", {})

        if action_name == "speak":
            message = params.get("message", "")
            log("Agent says: %s" % message, "blue")
            return False, False

        if action_name == "ask":
            answer = self._answer_question(action)
            if answer is None:
                raise RuntimeError("Agent requested more information: %s" % params.get("question", ""))
            log("Auto-answering %s => %s" % (params.get("info_name", ""), answer), "yellow")
            returned_action = self.mobile_gpt.set_qa_answer(
                params.get("info_name", ""),
                params.get("question", ""),
                str(answer),
            )
            follow_up_actions, finished = self._collect_messages(returned_action)
            action_executed = False
            for follow_up_action in follow_up_actions:
                executed, finished_now = self._execute_action(follow_up_action, raw_xml_path)
                action_executed = action_executed or executed
                finished = finished or finished_now
                if finished:
                    break
            return action_executed, finished

        if action_name in ["finish", "stop"]:
            return False, True

        bbox = self._resolve_target_bbox(action, raw_xml_path)
        center = _center_of_bbox(bbox)

        if action_name == "click":
            if center is None:
                raise RuntimeError("Could not resolve click target for action %s" % json.dumps(action))
            self.controller.tap(center[0], center[1])
            return True, False

        if action_name == "repeat-click":
            if center is None:
                raise RuntimeError("Could not resolve repeat-click target for action %s" % json.dumps(action))
            repeat_count = _safe_int(params.get("number", 1), 1)
            for _ in range(max(1, repeat_count)):
                self.controller.tap(center[0], center[1])
                time.sleep(0.2)
            return True, False

        if action_name == "long-click":
            if center is None:
                raise RuntimeError("Could not resolve long-click target for action %s" % json.dumps(action))
            self.controller.long_press(center[0], center[1], duration_ms=1200)
            return True, False

        if action_name == "input":
            input_text = params.get("input_text", "")
            if center is not None:
                self.controller.tap(center[0], center[1])
                time.sleep(0.4)
            self.controller.input_text(input_text)
            return True, False

        if action_name == "scroll":
            direction = params.get("direction", "down")
            start_xy, end_xy = self._resolve_scroll_points(bbox, direction)
            self.controller.swipe(start_xy, end_xy, duration_ms=400)
            return True, False

        if action_name in ["back", "go-back"]:
            self.controller.keyevent("KEYCODE_BACK")
            return True, False

        raise RuntimeError("Unsupported action in direct ADB mode: %s" % json.dumps(action))

    def run(self):
        task, target_package, log_directory = self._prepare_session()
        self._ensure_app_started()
        log("Direct ADB mode started for %s / %s" % (target_package, task["name"]), "green")
        log("Task: %s" % self.task_instruction, "green")

        finished = False
        screen_count = 0
        try:
            for step in range(self.max_steps):
                self.guard_adapter.complete_pending_action()

                screenshot_path = os.path.join(log_directory, "screenshots", "%d.jpg" % screen_count)
                raw_xml_path = os.path.join(log_directory, "xmls", "%d.xml" % screen_count)

                self.controller.capture_screenshot(screenshot_path)
                raw_xml = self.controller.dump_ui_xml(raw_xml_path)
                current_package = _extract_current_package(raw_xml)
                parsed_xml, hierarchy_xml, encoded_xml = self.screen_parser.encode(raw_xml, screen_count)

                self.guard_adapter.set_screen_context(
                    screen_index=screen_count,
                    screenshot_path=screenshot_path,
                    raw_xml_path=raw_xml_path,
                    parsed_xml_path=os.path.join(log_directory, "xmls", "%d_parsed.xml" % screen_count),
                    hierarchy_xml_path=os.path.join(log_directory, "xmls", "%d_hierarchy_parsed.xml" % screen_count),
                    encoded_xml_path=os.path.join(log_directory, "xmls", "%d_encoded.xml" % screen_count),
                    current_package=current_package,
                    current_activity=self.activity_name,
                )
                screen_count += 1

                returned_action = self.mobile_gpt.get_next_action(parsed_xml, hierarchy_xml, encoded_xml)
                actions, finished = self._collect_messages(returned_action)

                action_executed = False
                for action in actions:
                    executed, finished_now = self._execute_action(action, raw_xml_path)
                    action_executed = action_executed or executed
                    finished = finished or finished_now
                    if finished:
                        break

                if finished:
                    break

                if not action_executed:
                    raise RuntimeError("Agent did not emit any executable action on step %d." % (step + 1))

                time.sleep(self.action_wait)

            else:
                raise RuntimeError("Reached MOBILEGPT_ADB_MAX_STEPS=%d without finishing the task." % self.max_steps)
        finally:
            self.guard_adapter.complete_pending_action()
            self.guard_adapter.cleanup_trace_runtime()

        log("Direct ADB mode completed successfully.", "green")
        return True
