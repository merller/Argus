import os, sys
import argparse
from dotenv import load_dotenv
from server import Server
from server_explore import Explorer

# os.chdir('./MobileGPT_server')
sys.path.append('.')

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("--backdoor-mode", default=os.getenv("MOBILEGPT_BACKDOOR_MODE", "text"))
parser.add_argument("--backdoor-autotap", action="store_true")
parser.add_argument("--guard-method", default=os.getenv("MOBILEGPT_GUARD_METHOD_NAME", os.getenv("GUARD_METHOD_NAME", "")))
parser.add_argument("--guard-script", default=os.getenv("MOBILEGPT_GUARD_SCRIPT_PATH", ""))
parser.add_argument("--guard-run-external", action="store_true")
parser.add_argument("--guard-export", action="store_true")
args, _ = parser.parse_known_args()

if args.backdoor_mode:
    os.environ["MOBILEGPT_BACKDOOR_MODE"] = args.backdoor_mode
if args.backdoor_autotap:
    os.environ["MOBILEGPT_BACKDOOR_AUTOTAP_ENABLED"] = "true"
if args.guard_method:
    os.environ["MOBILEGPT_GUARD_METHOD_NAME"] = args.guard_method
if args.guard_script:
    os.environ["MOBILEGPT_GUARD_SCRIPT_PATH"] = args.guard_script
if args.guard_run_external:
    os.environ["MOBILEGPT_GUARD_RUN_EXTERNAL"] = "true"
    os.environ["MOBILEGPT_GUARD_EXPORT_ENABLED"] = "true"
if args.guard_export:
    os.environ["MOBILEGPT_GUARD_EXPORT_ENABLED"] = "true"


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ["1", "true", "yes", "y", "on"]


def _setdefault_from_env(target_name, *source_names):
    if os.getenv(target_name):
        return os.getenv(target_name)
    for source_name in source_names:
        value = os.getenv(source_name)
        if value:
            os.environ[target_name] = value
            return value
    return None


_setdefault_from_env("OPENAI_BASE_URL", "API_BASE", "OPENAI_API_BASE")
_setdefault_from_env("OPENAI_API_KEY", "CLAUDE_API_KEY", "GPT4O_API_KEY", "APIKey")
_setdefault_from_env("OPENAI_EMBEDDING_API_KEY", "GPT4O_API_KEY", "OPENAI_API_KEY", "APIKey")
_setdefault_from_env("OPENAI_EMBEDDING_BASE_URL", "API_BASE", "OPENAI_BASE_URL")
_setdefault_from_env("OPENAI_EMBEDDING_MODEL", "EMB_MODEL")
_setdefault_from_env("MOBILEGPT_GUARD_DEVICE", "SERIAL")
_setdefault_from_env("MOBILEGPT_GUARD_ADB_PATH", "ADB")
_setdefault_from_env("MOBILEGPT_GUARD_METHOD_NAME", "GUARD_METHOD_NAME", "DEFENSE_METHOD")
_setdefault_from_env("MOBILEGPT_GUARD_SCRIPT_PATH", "GUARD_SCRIPT_PATH", "DEFENSE_SCRIPT")
_setdefault_from_env("MOBILEGPT_GUARD_ALLOWED_PACKAGE", "PKG")
_setdefault_from_env("MOBILEGPT_GUARD_ALLOWED_ACTIVITY_PREFIX", "ACT_PREFIX")
_setdefault_from_env("MOBILEGPT_GUARD_APP_NAME", "APPAGENT_NAME")
_setdefault_from_env("MOBILEGPT_GUARD_TRACE_WRAP_ACTIVITY", "ACTIVITY")
_setdefault_from_env("MOBILEGPT_GUARD_TRACE_WRAP_PACKAGE", "PKG")

default_model = (
    os.getenv("OPENAI_MODEL_NAME")
    or os.getenv("TEXT_MODEL")
    or os.getenv("VISION_MODEL")
    or "gpt-4o"
)
default_reasoning_model = os.getenv("OPENAI_REASONING_MODEL") or os.getenv("TEXT_MODEL") or default_model
default_vision_model = os.getenv("OPENAI_VISION_MODEL") or os.getenv("VISION_MODEL") or default_model

os.environ.setdefault("OPENAI_MODEL_NAME", default_model)
os.environ.setdefault("OPENAI_REASONING_MODEL", default_reasoning_model)
os.environ.setdefault("OPENAI_VISION_MODEL", default_vision_model)

os.environ.setdefault("TASK_AGENT_GPT_VERSION", default_model)
os.environ.setdefault("APP_AGENT_GPT_VERSION", default_model)
os.environ.setdefault("SELECT_AGENT_HISTORY_GPT_VERSION", default_model)
os.environ.setdefault("EXPLORE_AGENT_GPT_VERSION", default_model)
os.environ.setdefault("SELECT_AGENT_GPT_VERSION", default_reasoning_model)
os.environ.setdefault("DERIVE_AGENT_GPT_VERSION", default_reasoning_model)
os.environ.setdefault("PARAMETER_FILLER_AGENT_GPT_VERSION", default_model)
os.environ.setdefault("ACTION_SUMMARIZE_AGENT_GPT_VERSION", default_model)
os.environ.setdefault("SUBTASK_MERGE_AGENT_GPT_VERSION", default_model)

os.environ.setdefault("gpt_4", default_model)
os.environ.setdefault("gpt_4_turbo", default_reasoning_model)
os.environ.setdefault("gpt_3_5_turbo", default_model)

os.environ.setdefault("vision_model", default_vision_model)
os.environ.setdefault("MOBILEGPT_USER_NAME", "user")


def main():
    if _env_flag("MOBILEGPT_USE_ADB_DIRECT", False):
        from adb_runner import AdbDirectRunner
        runner = AdbDirectRunner()
        runner.run()
        return

    server_ip = "0.0.0.0"
    server_port = 12345
    server_vision = False

    mobilGPT_server = Server(host=server_ip, port=int(server_port), buffer_size=4096)
    mobilGPT_server.open()

    # mobilGPT_explorer = Explorer(host=server_ip, port=int(server_port), buffer_size=4096)
    # mobilGPT_explorer.open()


if __name__ == '__main__':
    main()
