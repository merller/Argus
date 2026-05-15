import os
import yaml


def load_config(config_path="./config.yaml"):
    with open(config_path, "r") as file:
        yaml_data = yaml.safe_load(file) or {}

    configs = dict(yaml_data)
    configs.update(os.environ)

    alias_map = {
        "OPENAI_API_KEY": ["APIKey", "OPENAI_API_KEY", "THIRD_PARTY_API_KEY"],
        "OPENAI_API_BASE": [
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "THIRD_PARTY_OPENAI_BASE_URL",
            "THIRD_PARTY_API_BASE",
        ],
        "OPENAI_API_MODEL": ["OPENAI_MODEL_NAME", "OPENAI_MODEL", "MODEL_NAME"],
        "ADB_PATH": ["ADB_PATH", "ADB"],
        "GUARD_PYTHON_EXEC": ["GUARD_PYTHON_EXEC", "GUARD_PY", "APP_PY"],
        "GUARD_METHOD_NAME": ["GUARD_METHOD_NAME", "DEFENSE_METHOD", "APPAGENT_GUARD_METHOD"],
        "GUARD_SCRIPT_PATH": ["GUARD_SCRIPT_PATH", "DEFENSE_SCRIPT", "APPAGENT_GUARD_SCRIPT_PATH"],
        "GUARD_ALLOWED_PACKAGE": ["GUARD_ALLOWED_PACKAGE", "PKG", "APP_PACKAGE"],
        "DEVICE_TRACE_WRAP_PACKAGE": ["DEVICE_TRACE_WRAP_PACKAGE", "PKG", "APP_PACKAGE"],
        "DEVICE_TRACE_WRAP_ACTIVITY": ["DEVICE_TRACE_WRAP_ACTIVITY", "ACTIVITY", "APP_ACTIVITY"],
        "GUARD_ALLOWED_ACTIVITY_PREFIX": ["GUARD_ALLOWED_ACTIVITY_PREFIX", "ACT_PREFIX", "ACTIVITY_PREFIX"],
        "DEVICE_TRACE_TOOL": ["DEVICE_TRACE_TOOL", "TRACE_TOOL"],
    }

    for target_key, candidate_keys in alias_map.items():
        for candidate_key in candidate_keys:
            value = os.environ.get(candidate_key)
            if value:
                configs[target_key] = value
                break

    return configs
