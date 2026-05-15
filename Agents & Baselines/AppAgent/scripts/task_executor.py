import argparse
import ast
import json
import os
import re
import sys
import time

import prompts
from config import load_config
from and_controller import (
    list_all_devices,
    AndroidController,
    traverse_tree,
)
from device_trace import (
    cleanup_device_trace_runtime,
    prepare_device_trace_runtime,
    resolve_trace_packages,
    start_device_trace,
    stop_device_trace,
)
from guard_adapter import export_guard_input, run_external_guard, attach_execution_trace, get_guard_method_name_from_configs
from backdoor_hook import maybe_tap_backdoor_button
from model import parse_explore_rsp, parse_grid_rsp, OpenAIModel, QwenModel
from utils import print_with_color, draw_bbox_multi, draw_grid

arg_desc = "AppAgent Executor"
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
parser.add_argument("--app")
parser.add_argument("--root_dir", default="./")
parser.add_argument("--task", default="")
args = vars(parser.parse_args())

configs = load_config()

if configs["MODEL"] == "OpenAI":
    mllm = OpenAIModel(base_url=configs["OPENAI_API_BASE"],
                       api_key=configs["OPENAI_API_KEY"],
                       model=configs["OPENAI_API_MODEL"],
                       temperature=configs["TEMPERATURE"],
                       max_tokens=configs["MAX_TOKENS"])
elif configs["MODEL"] == "Qwen":
    mllm = QwenModel(api_key=configs["DASHSCOPE_API_KEY"],
                     model=configs["QWEN_MODEL"])
else:
    print_with_color(f"ERROR: Unsupported model type {configs['MODEL']}!", "red")
    sys.exit()

app = args["app"]
root_dir = args["root_dir"]
task_desc = args["task"] or os.environ.get("APPAGENT_TASK", "")

if not app:
    print_with_color("What is the name of the app you want me to operate?", "blue")
    app = input()
    app = app.replace(" ", "")

app_dir = os.path.join(os.path.join(root_dir, "apps"), app)
work_dir = os.path.join(root_dir, "tasks")
os.makedirs(work_dir, exist_ok=True)
auto_docs_dir = os.path.join(app_dir, "auto_docs")
demo_docs_dir = os.path.join(app_dir, "demo_docs")
dir_name = "appagent"
task_dir = os.path.join(work_dir, dir_name)
os.makedirs(task_dir, exist_ok=True)
log_path = os.path.join(task_dir, f"{dir_name}.txt")
with open(log_path, "w", encoding="utf-8"):
    pass

no_doc = False
if not os.path.exists(auto_docs_dir) and not os.path.exists(demo_docs_dir):
    print_with_color(f"No documentations found for the app {app}. Do you want to proceed with no docs? Enter y or n",
                     "red")
    user_input = ""
    while user_input != "y" and user_input != "n":
        user_input = input().lower()
    if user_input == "y":
        no_doc = True
    else:
        sys.exit()
elif os.path.exists(auto_docs_dir) and os.path.exists(demo_docs_dir):
    print_with_color(f"The app {app} has documentations generated from both autonomous exploration and human "
                     f"demonstration. Which one do you want to use? Type 1 or 2.\n1. Autonomous exploration\n2. Human "
                     f"Demonstration",
                     "blue")
    user_input = ""
    while user_input != "1" and user_input != "2":
        user_input = input()
    if user_input == "1":
        docs_dir = auto_docs_dir
    else:
        docs_dir = demo_docs_dir
elif os.path.exists(auto_docs_dir):
    print_with_color(f"Documentations generated from autonomous exploration were found for the app {app}. The doc base "
                     f"is selected automatically.", "yellow")
    docs_dir = auto_docs_dir
else:
    print_with_color(f"Documentations generated from human demonstration were found for the app {app}. The doc base is "
                     f"selected automatically.", "yellow")
    docs_dir = demo_docs_dir

device_list = list_all_devices()
if not device_list:
    print_with_color("ERROR: No device found!", "red")
    sys.exit()
print_with_color(f"List of devices attached:\n{str(device_list)}", "yellow")
if len(device_list) == 1:
    device = device_list[0]
    print_with_color(f"Device selected: {device}", "yellow")
else:
    print_with_color("Please choose the Android device to start demo by entering its ID:", "blue")
    device = input()
controller = AndroidController(device)
width, height = controller.get_device_size()
if not width and not height:
    print_with_color("ERROR: Invalid device size!", "red")
    sys.exit()
print_with_color(f"Screen resolution of {device}: {width}x{height}", "yellow")

if not task_desc:
    print_with_color("Please enter the description of the task you want me to complete in a few sentences:", "blue")
    task_desc = input()

if configs.get("DEVICE_TRACE_ENABLED", False):
    prepare_device_trace_runtime(
        configs=configs,
        device=device,
        packages=resolve_trace_packages(
            configs=configs,
            current_package=str(configs.get("DEVICE_TRACE_WRAP_PACKAGE", "") or ""),
            target_package=str(configs.get("GUARD_ALLOWED_PACKAGE", "") or ""),
            current_activity=str(configs.get("DEVICE_TRACE_WRAP_ACTIVITY", "") or ""),
        ),
    )

round_count = 0
last_act = "None"
task_complete = False
grid_on = False
rows, cols = 0, 0


def area_to_xy(area, subarea):
    area -= 1
    row, col = area // cols, area % cols
    x_0, y_0 = col * (width // cols), row * (height // rows)
    if subarea == "top-left":
        x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) // 4
    elif subarea == "top":
        x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) // 4
    elif subarea == "top-right":
        x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) // 4
    elif subarea == "left":
        x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) // 2
    elif subarea == "right":
        x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) // 2
    elif subarea == "bottom-left":
        x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) * 3 // 4
    elif subarea == "bottom":
        x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) * 3 // 4
    elif subarea == "bottom-right":
        x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) * 3 // 4
    else:
        x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) // 2
    return x, y


while round_count < configs["MAX_ROUNDS"]:
    round_count += 1
    print_with_color(f"Round {round_count}", "yellow")
    artifact_name = f"{dir_name}_{round_count}"
    screenshot_path = controller.get_screenshot(artifact_name, task_dir)
    xml_path = controller.get_xml(artifact_name, task_dir)
    if screenshot_path == "ERROR" or xml_path == "ERROR":
        break
    maybe_tap_backdoor_button(configs, device, xml_path)
    if grid_on:
        image = os.path.join(task_dir, f"{artifact_name}_grid.png")
        rows, cols = draw_grid(screenshot_path, image)
        prompt = prompts.task_template_grid
    else:
        clickable_list = []
        focusable_list = []
        traverse_tree(xml_path, clickable_list, "clickable", True)
        traverse_tree(xml_path, focusable_list, "focusable", True)
        elem_list = clickable_list.copy()
        for elem in focusable_list:
            bbox = elem.bbox
            center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
            close = False
            for e in clickable_list:
                bbox = e.bbox
                center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                if dist <= configs["MIN_DIST"]:
                    close = True
                    break
            if not close:
                elem_list.append(elem)
        image = os.path.join(task_dir, f"{artifact_name}_labeled.png")
        draw_bbox_multi(screenshot_path, image, elem_list, dark_mode=configs["DARK_MODE"])
        if no_doc:
            prompt = re.sub(r"<ui_document>", "", prompts.task_template)
        else:
            ui_doc = ""
            for i, elem in enumerate(elem_list):
                doc_path = os.path.join(docs_dir, f"{elem.uid}.txt")
                if not os.path.exists(doc_path):
                    continue
                ui_doc += f"Documentation of UI element labeled with the numeric tag '{i + 1}':\n"
                doc_content = ast.literal_eval(open(doc_path, "r").read())
                if doc_content["tap"]:
                    ui_doc += f"This UI element is clickable. {doc_content['tap']}\n\n"
                if doc_content["text"]:
                    ui_doc += f"This UI element can receive text input. The text input is used for the following " \
                              f"purposes: {doc_content['text']}\n\n"
                if doc_content["long_press"]:
                    ui_doc += f"This UI element is long clickable. {doc_content['long_press']}\n\n"
                if doc_content["v_swipe"]:
                    ui_doc += f"This element can be swiped directly without tapping. You can swipe vertically on " \
                              f"this UI element. {doc_content['v_swipe']}\n\n"
                if doc_content["h_swipe"]:
                    ui_doc += f"This element can be swiped directly without tapping. You can swipe horizontally on " \
                              f"this UI element. {doc_content['h_swipe']}\n\n"
            print_with_color(f"Documentations retrieved for the current interface:\n{ui_doc}", "magenta")
            ui_doc = """
            You also have access to the following documentations that describes the functionalities of UI 
            elements you can interact on the screen. These docs are crucial for you to determine the target of your 
            next action. You should always prioritize these documented elements for interaction:""" + ui_doc
            prompt = re.sub(r"<ui_document>", ui_doc, prompts.task_template)
    prompt = re.sub(r"<task_description>", task_desc, prompt)
    prompt = re.sub(r"<last_act>", last_act, prompt)
    print_with_color("Thinking about what to do in the next step...", "yellow")
    status, rsp = mllm.get_model_response(prompt, [image])

    if status:
        with open(log_path, "a", encoding="utf-8") as logfile:
            log_item = {"step": round_count, "prompt": prompt, "image": os.path.basename(image),
                        "response": rsp}
            logfile.write(json.dumps(log_item) + "\n")
        if grid_on:
            res = parse_grid_rsp(rsp)
        else:
            res = parse_explore_rsp(rsp)
        act_name = res[0]
        if act_name == "FINISH":
            task_complete = True
            break
        if act_name == "ERROR":
            break
        last_act = res[-1]
        res = res[:-1]
        export_info = export_guard_input(configs=configs,
                                         device=device,
                                         app=app,
                                         task_desc=task_desc,
                                         round_count=round_count,
                                         screenshot_path=screenshot_path,
                                         xml_path=xml_path,
                                         response_text=rsp,
                                         act_name=act_name,
                                         act_params=res,
                                         elem_list=elem_list if not grid_on else None,
                                         task_dir=task_dir)
        if export_info and not configs.get("GUARD_RUN_AFTER_ACTION", False):
            run_external_guard(configs=configs,
                               device=device,
                               task_desc=task_desc,
                               action_json_path=export_info["json_path"],
                               task_dir=task_dir,
                               round_count=round_count)
            if configs.get("GUARD_STOP_AFTER_EXPORT", False):
                print_with_color("Stopped after exporting guard input as requested by config.", "yellow")
                break
        trace_session = None
        should_trace_action = bool(configs.get("DEVICE_TRACE_ENABLED", False) and export_info and act_name != "grid")
        if should_trace_action:
            guard_method = get_guard_method_name_from_configs(configs)
            trace_dir = os.path.join(task_dir, configs.get("DEVICE_TRACE_DIR_NAME", "syscall_traces"), guard_method)
            payload = export_info["payload"]
            action_payload = payload.get("agent_action", {})
            trace_packages = resolve_trace_packages(
                configs=configs,
                current_package=payload.get("current_package", ""),
                target_package=action_payload.get("target_package", ""),
                current_activity=payload.get("current_activity", ""),
            )
            print_with_color(
                f"[{guard_method}] Starting device-side syscall trace for packages: {trace_packages}",
                "cyan",
            )
            trace_session = start_device_trace(
                configs=configs,
                device=device,
                trace_dir=trace_dir,
                trace_name=f"step_{round_count:03d}_{act_name}",
                packages=trace_packages,
            )
        ret = None
        print_with_color(f"Executing action: {act_name}", "cyan")
        if act_name == "tap":
            _, area = res
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.tap(x, y)
            if ret == "ERROR":
                print_with_color("ERROR: tap execution failed", "red")
                break
        elif act_name == "text":
            _, input_str = res
            ret = controller.text(input_str)
            if ret == "ERROR":
                print_with_color("ERROR: text execution failed", "red")
                break
        elif act_name == "long_press":
            _, area = res
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.long_press(x, y)
            if ret == "ERROR":
                print_with_color("ERROR: long press execution failed", "red")
                break
        elif act_name == "swipe":
            _, area, swipe_dir, dist = res
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.swipe(x, y, swipe_dir, dist)
            if ret == "ERROR":
                print_with_color("ERROR: swipe execution failed", "red")
                break
        elif act_name == "grid":
            grid_on = True
        elif act_name == "tap_grid" or act_name == "long_press_grid":
            _, area, subarea = res
            x, y = area_to_xy(area, subarea)
            if act_name == "tap_grid":
                ret = controller.tap(x, y)
                if ret == "ERROR":
                    print_with_color("ERROR: tap execution failed", "red")
                    break
            else:
                ret = controller.long_press(x, y)
                if ret == "ERROR":
                    print_with_color("ERROR: tap execution failed", "red")
                    break
        elif act_name == "swipe_grid":
            _, start_area, start_subarea, end_area, end_subarea = res
            start_x, start_y = area_to_xy(start_area, start_subarea)
            end_x, end_y = area_to_xy(end_area, end_subarea)
            ret = controller.swipe_precise((start_x, start_y), (end_x, end_y))
            if ret == "ERROR":
                print_with_color("ERROR: tap execution failed", "red")
                break
        if should_trace_action:
            print_with_color(f"[{guard_method}] Stopping device-side syscall trace...", "cyan")
            trace_info = stop_device_trace(configs=configs, device=device, session=trace_session)
            if export_info and trace_info:
                attach_execution_trace(export_info["json_path"], trace_info)
        if export_info and configs.get("GUARD_RUN_AFTER_ACTION", False) and act_name != "grid":
            print_with_color(
                f"[{get_guard_method_name_from_configs(configs)}] Running external guard after action...",
                "cyan",
            )
            run_external_guard(configs=configs,
                               device=device,
                               task_desc=task_desc,
                               action_json_path=export_info["json_path"],
                               task_dir=task_dir,
                               round_count=round_count)
        if act_name != "grid":
            grid_on = False
        time.sleep(configs["REQUEST_INTERVAL"])
    else:
        print_with_color(rsp, "red")
        break

if configs.get("DEVICE_TRACE_ENABLED", False):
    cleanup_device_trace_runtime(configs=configs, device=device)

if task_complete:
    print_with_color("Task completed successfully", "yellow")
elif round_count == configs["MAX_ROUNDS"]:
    print_with_color("Task finished due to reaching max rounds", "yellow")
else:
    print_with_color("Task finished unexpectedly", "red")
