import os, sys
from dotenv import load_dotenv
from server import Server
from server_explore import Explorer

# os.chdir('./MobileGPT_server')
sys.path.append('.')

load_dotenv()
default_model = os.getenv("OPENAI_MODEL_NAME", "gpt-4o")
default_reasoning_model = os.getenv("OPENAI_REASONING_MODEL", default_model)
default_vision_model = os.getenv("OPENAI_VISION_MODEL", default_model)

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
    server_ip = "0.0.0.0"
    server_port = 12345
    server_vision = False

    mobilGPT_server = Server(host=server_ip, port=int(server_port), buffer_size=4096)
    mobilGPT_server.open()

    # mobilGPT_explorer = Explorer(host=server_ip, port=int(server_port), buffer_size=4096)
    # mobilGPT_explorer.open()


if __name__ == '__main__':
    main()
