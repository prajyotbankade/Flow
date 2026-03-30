import sys
from pathlib import Path

# Ensure eval modules are importable when pytest runs from the project root
sys.path.insert(0, str(Path(__file__).parent))


def pytest_sessionfinish(session, exitstatus):
    """Print token usage summary at end of test session."""
    from eval_flow_skill import get_token_usage, EVAL_LLM

    gen = get_token_usage().get("generation", {})
    gen_prompt = gen.get("prompt_tokens", 0)
    gen_completion = gen.get("completion_tokens", 0)
    gen_calls = gen.get("calls", 0)
    gen_total = gen_prompt + gen_completion

    backend = "OpenAI gpt-4o-mini" if EVAL_LLM == "openai" else "Ollama qwen 7b"

    print("\n")
    print("=" * 60)
    print(f"TOKEN USAGE SUMMARY (generation via {backend})")
    print("=" * 60)
    print(f"  Prompt tokens:     {gen_prompt:,}")
    print(f"  Completion tokens: {gen_completion:,}")
    print(f"  Total tokens:      {gen_total:,}")
    print(f"  API calls:         {gen_calls}")
    print("=" * 60)
