import glob
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from config import load_config
from utils import print_with_color


configs = load_config()
_trace_context = None
_last_trace_info = None


class AndroidElement:
    def __init__(self, uid, bbox, attrib, raw_attrib=None):
        self.uid = uid
        self.bbox = bbox
        self.attrib = attrib
        self.raw_attrib = raw_attrib or {}


def execute_adb(adb_command):
    global _last_trace_info
    adb_path = configs.get("ADB_PATH", "adb")
    if adb_command == "adb":
        adb_command = f'"{adb_path}"'
    elif adb_command.startswith("adb "):
        adb_command = f'"{adb_path}"{adb_command[3:]}'

    wrapped_command = adb_command
    trace_enabled = bool(configs.get("ADB_TRACE_ENABLED", False) and _trace_context and adb_command.startswith('"'))
    if trace_enabled:
        trace_prefix = _trace_context["prefix"]
        strace_tool = configs.get("ADB_TRACE_TOOL", "strace")
        trace_filter = configs.get(
            "ADB_TRACE_FILTER",
            "execve,openat,openat2,rename,renameat,renameat2,unlink,unlinkat,connect,accept,sendto,recvfrom,sendmsg,recvmsg",
        )
        trace_string_size = int(configs.get("ADB_TRACE_STRING_SIZE", 256))
        wrapped_command = (
            f'{strace_tool} -ff -tt -s {trace_string_size} -e trace={trace_filter} '
            f'-o "{trace_prefix}" {adb_command}'
        )

    result = subprocess.run(wrapped_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if trace_enabled:
        trace_files = sorted(glob.glob(trace_prefix + "*"))
        _last_trace_info = {
            "command": adb_command,
            "wrapped_command": wrapped_command,
            "trace_prefix": trace_prefix,
            "trace_files": [os.path.abspath(path) for path in trace_files],
            "captured_at": int(time.time()),
            "returncode": result.returncode,
        }
    if result.returncode == 0:
        stdout_text = result.stdout.strip()
        stderr_text = result.stderr.strip()
        if stdout_text and stderr_text:
            return stdout_text + "\n" + stderr_text
        if stdout_text:
            return stdout_text
        if stderr_text:
            return stderr_text
        return ""
    print_with_color(f"Command execution failed: {adb_command}", "red")
    if result.stdout.strip():
        print_with_color(result.stdout, "red")
    if result.stderr.strip():
        print_with_color(result.stderr, "red")
    return "ERROR"


def set_adb_trace_context(trace_dir, trace_name):
    global _trace_context, _last_trace_info
    os.makedirs(trace_dir, exist_ok=True)
    _trace_context = {"prefix": os.path.join(trace_dir, trace_name)}
    _last_trace_info = None


def clear_adb_trace_context():
    global _trace_context
    _trace_context = None


def get_last_adb_trace():
    return _last_trace_info


def list_all_devices():
    adb_command = "adb devices"
    device_list = []
    result = execute_adb(adb_command)
    if result != "ERROR":
        devices = result.split("\n")[1:]
        for d in devices:
            d = d.strip()
            if not d:
                continue
            fields = d.split()
            if len(fields) >= 2 and fields[1] == "device":
                device_list.append(fields[0])

    return device_list


def get_id_from_element(elem):
    bounds = elem.attrib["bounds"][1:-1].split("][")
    x1, y1 = map(int, bounds[0].split(","))
    x2, y2 = map(int, bounds[1].split(","))
    elem_w, elem_h = x2 - x1, y2 - y1
    if "resource-id" in elem.attrib and elem.attrib["resource-id"]:
        elem_id = elem.attrib["resource-id"].replace(":", ".").replace("/", "_")
    else:
        elem_id = f"{elem.attrib['class']}_{elem_w}_{elem_h}"
    if "content-desc" in elem.attrib and elem.attrib["content-desc"] and len(elem.attrib["content-desc"]) < 20:
        content_desc = elem.attrib['content-desc'].replace("/", "_").replace(" ", "").replace(":", "_")
        elem_id += f"_{content_desc}"
    return elem_id


def traverse_tree(xml_path, elem_list, attrib, add_index=False):
    path = []
    for event, elem in ET.iterparse(xml_path, ['start', 'end']):
        if event == 'start':
            path.append(elem)
            if attrib in elem.attrib and elem.attrib[attrib] == "true":
                parent_prefix = ""
                if len(path) > 1:
                    parent_prefix = get_id_from_element(path[-2])
                bounds = elem.attrib["bounds"][1:-1].split("][")
                x1, y1 = map(int, bounds[0].split(","))
                x2, y2 = map(int, bounds[1].split(","))
                center = (x1 + x2) // 2, (y1 + y2) // 2
                elem_id = get_id_from_element(elem)
                if parent_prefix:
                    elem_id = parent_prefix + "_" + elem_id
                if add_index:
                    elem_id += f"_{elem.attrib['index']}"
                close = False
                for e in elem_list:
                    bbox = e.bbox
                    center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                    if dist <= configs["MIN_DIST"]:
                        close = True
                        break
                if not close:
                    elem_list.append(AndroidElement(elem_id, ((x1, y1), (x2, y2)), attrib, dict(elem.attrib)))

        if event == 'end':
            path.pop()


def _extract_dumped_xml_path(output):
    if not output or output == "ERROR":
        return ""
    match = re.search(r"dumped to:\s*(\S+)", output, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_xml_payload(output):
    if not output or output == "ERROR":
        return ""

    xml_start = output.find("<?xml")
    if xml_start >= 0:
        return output[xml_start:].strip()

    hierarchy_start = output.find("<hierarchy")
    if hierarchy_start >= 0:
        return output[hierarchy_start:].strip()

    return ""


class AndroidController:
    def __init__(self, device):
        self.device = device
        self.screenshot_dir = configs["ANDROID_SCREENSHOT_DIR"]
        self.xml_dir = configs["ANDROID_XML_DIR"]
        self.width, self.height = self.get_device_size()
        self.backslash = "\\"

    def get_device_size(self):
        adb_command = f"adb -s {self.device} shell wm size"
        result = execute_adb(adb_command)
        if result != "ERROR":
            return map(int, result.split(": ")[1].split("x"))
        return 0, 0

    def get_screenshot(self, prefix, save_dir):
        cap_command = f"adb -s {self.device} shell screencap -p " \
                      f"{os.path.join(self.screenshot_dir, prefix + '.png').replace(self.backslash, '/')}"
        pull_command = f"adb -s {self.device} pull " \
                       f"{os.path.join(self.screenshot_dir, prefix + '.png').replace(self.backslash, '/')} " \
                       f"{os.path.join(save_dir, prefix + '.png')}"
        result = execute_adb(cap_command)
        if result != "ERROR":
            result = execute_adb(pull_command)
            if result != "ERROR":
                return os.path.join(save_dir, prefix + ".png")
            return result
        return result

    def get_xml(self, prefix, save_dir):
        requested_remote_path = os.path.join(self.xml_dir, prefix + '.xml').replace(self.backslash, '/')
        local_xml_path = os.path.join(save_dir, prefix + '.xml')
        dump_command = f"adb -s {self.device} shell uiautomator dump {requested_remote_path}"
        retry_count = int(configs.get("ANDROID_XML_RETRY_COUNT", 3))
        retry_wait = float(configs.get("ANDROID_XML_RETRY_WAIT", 1.0))
        fallback_remote_paths = [
            requested_remote_path,
            os.path.join(self.xml_dir, 'window_dump.xml').replace(self.backslash, '/'),
            '/sdcard/window_dump.xml',
            '/data/local/tmp/window_dump.xml',
        ]
        last_result = "ERROR"
        for attempt_idx in range(retry_count):
            result = execute_adb(dump_command)
            last_result = result
            if result == "ERROR":
                if attempt_idx < retry_count - 1:
                    time.sleep(retry_wait)
                    continue
                return result
            if "ERROR:" in result and "dumped to:" not in result.lower():
                last_result = "ERROR"
                if attempt_idx < retry_count - 1:
                    time.sleep(retry_wait)
                    continue
                return "ERROR"

            dump_reported_path = _extract_dumped_xml_path(result)
            remote_candidates = []
            if dump_reported_path:
                remote_candidates.append(dump_reported_path)
            remote_candidates.extend(fallback_remote_paths)

            seen = set()
            for remote_xml_path in remote_candidates:
                if not remote_xml_path or remote_xml_path in seen:
                    continue
                seen.add(remote_xml_path)
                pull_command = f"adb -s {self.device} pull {remote_xml_path} {local_xml_path}"
                pull_result = execute_adb(pull_command)
                last_result = pull_result
                if pull_result != "ERROR":
                    return local_xml_path
            stdout_dump_result = self._dump_xml_via_exec_out(local_xml_path)
            if stdout_dump_result != "ERROR":
                return stdout_dump_result
            if attempt_idx < retry_count - 1:
                time.sleep(retry_wait)
        return last_result

    def _dump_xml_via_exec_out(self, local_xml_path):
        adb_path = configs.get("ADB_PATH", "adb")
        cmd = [adb_path, "-s", self.device, "exec-out", "uiautomator", "dump", "/dev/tty"]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            print_with_color(f"Command execution failed: {' '.join(cmd)}", "red")
            print_with_color(str(exc), "red")
            return "ERROR"

        combined_output = "\n".join(
            [part for part in [result.stdout.strip(), result.stderr.strip()] if part]
        )
        if result.returncode != 0:
            if combined_output:
                print_with_color(combined_output, "red")
            return "ERROR"

        xml_payload = _extract_xml_payload(combined_output)
        if not xml_payload:
            return "ERROR"

        with open(local_xml_path, "w", encoding="utf-8") as xml_file:
            xml_file.write(xml_payload)
        return local_xml_path

    def back(self):
        adb_command = f"adb -s {self.device} shell input keyevent KEYCODE_BACK"
        ret = execute_adb(adb_command)
        return ret

    def tap(self, x, y):
        adb_command = f"adb -s {self.device} shell input tap {x} {y}"
        ret = execute_adb(adb_command)
        return ret

    def text(self, input_str):
        input_str = input_str.replace(" ", "%s")
        input_str = input_str.replace("'", "")
        adb_command = f"adb -s {self.device} shell input text {input_str}"
        ret = execute_adb(adb_command)
        return ret

    def long_press(self, x, y, duration=1000):
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x} {y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe(self, x, y, direction, dist="medium", quick=False):
        unit_dist = int(self.width / 10)
        if dist == "long":
            unit_dist *= 3
        elif dist == "medium":
            unit_dist *= 2
        if direction == "up":
            offset = 0, -2 * unit_dist
        elif direction == "down":
            offset = 0, 2 * unit_dist
        elif direction == "left":
            offset = -1 * unit_dist, 0
        elif direction == "right":
            offset = unit_dist, 0
        else:
            return "ERROR"
        duration = 100 if quick else 400
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x+offset[0]} {y+offset[1]} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe_precise(self, start, end, duration=400):
        start_x, start_y = start
        end_x, end_y = end
        adb_command = f"adb -s {self.device} shell input swipe {start_x} {start_y} {end_x} {end_y} {duration}"
        ret = execute_adb(adb_command)
        return ret
