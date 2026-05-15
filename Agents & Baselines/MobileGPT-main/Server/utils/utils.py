import inspect
import os, csv, re
import numpy as np
import json
import pandas as pd

from termcolor import colored
from openai import OpenAI
from typing import List
from ast import literal_eval

try:
    import httpx
except ImportError:
    httpx = None


def _get_first_env(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _normalize_openai_base_url(base_url: str):
    if not base_url:
        return None
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    return normalized


def _build_openai_client(key_names=None, base_url_names=None):
    key_names = key_names or ("OPENAI_API_KEY", "APIKey", "THIRD_PARTY_API_KEY")
    base_url_names = base_url_names or (
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "THIRD_PARTY_OPENAI_BASE_URL",
        "THIRD_PARTY_API_BASE",
    )

    api_key = _get_first_env(*key_names)
    if not api_key:
        raise RuntimeError(
            "Missing API key. Set one of: OPENAI_API_KEY, APIKey, THIRD_PARTY_API_KEY"
        )

    base_url = _normalize_openai_base_url(
        _get_first_env(*base_url_names)
    )

    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    # Compatibility path for environments where openai==1.11.1 is paired with
    # httpx>=0.28, whose Client constructor no longer accepts "proxies".
    if httpx is not None:
        try:
            supports_proxies_arg = "proxies" in inspect.signature(httpx.Client.__init__).parameters
        except (TypeError, ValueError):
            supports_proxies_arg = True
        if not supports_proxies_arg:
            client_kwargs["http_client"] = httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=300.0, write=60.0, connect=60.0),
            )

    return OpenAI(**client_kwargs)


def log(msg, color='white'):
    if not color:
        print(msg)
        return

    colored_log = colored(msg, color, attrs=['bold'])
    print(colored_log)
    print()


def safe_literal_eval(x):
    if pd.isna(x):
        return np.nan  # or return np.array([]) for converting NaN to empty arrays
    else:
        return np.array(literal_eval(x))


def get_openai_embedding(text: str, model=None, **kwargs) -> List[float]:
    client = _build_openai_client(
        key_names=(
            "OPENAI_EMBEDDING_API_KEY",
            "OPENAI_API_KEY",
            "APIKey",
            "THIRD_PARTY_API_KEY",
        ),
        base_url_names=(
            "OPENAI_EMBEDDING_BASE_URL",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "THIRD_PARTY_OPENAI_BASE_URL",
            "THIRD_PARTY_API_BASE",
        ),
    )
    model = model or _get_first_env(
        "OPENAI_EMBEDDING_MODEL",
        "EMBEDDING_MODEL_NAME",
        default="text-embedding-3-small",
    )
    # replace newlines, which can negatively affect performance.
    text = text.replace("\n", " ")

    response = client.embeddings.create(input=[text], model=model, **kwargs)

    return response.data[0].embedding


def cosine_similarity(a, b):
    if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    else:
        return 0


def generate_numbered_list(data: list) -> str:
    result_string = ""

    for index, item in enumerate(data, start=1):
        if isinstance(item, dict):
            result_string += f"- {json.dumps(item)}\n"
        else:
            result_string += f"- {item}\n"

    return result_string


def query(messages, model="gpt-4-turbo", is_list=False):
    client = _build_openai_client()
    model = model or _get_first_env("OPENAI_MODEL_NAME", "OPENAI_MODEL", "MODEL_NAME", default="gpt-4o")

    for message in messages:
        log("--------------------------")
        log(message["content"], 'yellow')
    # log("--------------------------")
    # log(messages[-1]["content"], 'yellow')

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=900,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0
    )
    result = response.choices[0].message.content
    log(result, 'green')
    json_formatted_response = __parse_json(result, is_list=is_list)
    if json_formatted_response:
        return json.loads(json_formatted_response)
    else:
        return result


def parse_completion_rate(completion_rate) -> int:
    # Convert the input to a string in case it's an integer
    input_str = str(completion_rate).strip()

    # Check if the string ends with a '%'
    if input_str.endswith('%'):
        # Remove the '%' and convert to integer
        return int(float(input_str[:-1]))
    else:
        # Convert to float to handle decimal or integer strings
        value = float(input_str)

        # If the value is less than 1, it's likely a decimal representation of a percentage
        if value < 1:
            return int(value * 100)
        # Otherwise, it's already in percentage form
        else:
            return int(value)


def __parse_json(s: str, is_list=False):
    if is_list:
        matches = re.search(r'\[.*\]', s, re.DOTALL)

        if matches:
            return matches.group(0)
    else:
        matches = re.search(r'\{.*\}', s, re.DOTALL)

        if matches:
            return matches.group(0)
