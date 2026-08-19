import json
import os
import re
import time
from typing import Any, Dict, Optional
from dotenv import load_dotenv
from json_repair import repair_json

# Load environment variables from .env or API.env if present
load_dotenv(".env")
load_dotenv("API.env")


def extract_and_parse_json(content: str) -> Dict[str, Any]:
    """Robustly extracts and repairs JSON content from LLM responses."""
    if not content:
        raise ValueError("Empty response content")

    # 1. Clean markdown code blocks
    cleaned = re.sub(r"```(?:json)?", "", content, flags=re.IGNORECASE).replace("```", "").strip()

    # 2. Try standard json.loads first
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 3. Use json_repair to handle trailing commas, unescaped quotes, or truncated JSON strings
    repaired = repair_json(cleaned, return_objects=True)
    if isinstance(repaired, dict) and repaired:
        return repaired

    # 4. Fallback: extract substring between first '{' and last '}'
    match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if match:
        repaired_sub = repair_json(match.group(1).strip(), return_objects=True)
        if isinstance(repaired_sub, dict) and repaired_sub:
            return repaired_sub

    raise ValueError("Could not repair/extract valid JSON from response")


def ping_model(client, model_name: str) -> bool:
    """
    Sends an ultra-lightweight 1-token health probe (costs ~0 tokens, 5.0s timeout).
    Returns True only if the model is alive and returns 200 OK.
    """
    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=2,
            timeout=5.0,
        )
        content = resp.choices[0].message.content
        return content is not None
    except Exception as e:
        print(f"[LLM Probe] '{model_name}' unreachable ({type(e).__name__})", flush=True)
        return False


def call_llm_api(system_prompt: str, user_prompt: str, context_text: str, selected_model: Optional[str] = None) -> Dict[str, Any]:
    """
    Pings each model in descending quality order with a 1-token probe before generating.
    If a model answers, generates the full plan; if not, immediately cascades down.
    """
    claudehub_key = os.getenv("CLAUDEHUB_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    full_system_instruction = (
        f"{system_prompt}\n\n"
        f"МАТЕРИАЛЫ БАЗЫ ЗНАНИЙ:\n"
        f"{context_text}\n\n"
        f"ВАЖНО: Верните результат СТРОГО в формате валидного JSON."
    )

    # Option 1: ClaudeHub API with Ping Probes (All top models included)
    if claudehub_key and claudehub_key != "your_claudehub_api_key_here":
        # 'auto' tries budget models first (cheap Qwen/DeepSeek), escalating to
        # premium only if cheaper ones are down; premium is used when picked explicitly.
        budget_hierarchy = [
            "qwen3.5-flash",        # 1. Ultra-lightweight & lowest token cost
            "deepseek-v4-flash",    # 2. Fast & economical DeepSeek v4 Flash
            "deepseek-v4-pro",      # 3. High-intelligence DeepSeek v4 Pro
            "claude-sonnet-5",      # 4. Premium fallback
            "claude-opus-5",        # 5. Flagship fallback
        ]
        premium_hierarchy = [
            "claude-sonnet-5",      # 1. Primary working flagship (fast 2.8s ping, 7s full generation)
            "claude-opus-5",        # 2. Flagship Opus 5
            "deepseek-v4-pro",      # 3. High-intelligence DeepSeek v4 Pro
            "deepseek-v4-flash",    # 4. Fast & economical DeepSeek v4 Flash
            "qwen3.5-flash",        # 5. Ultra-lightweight & lowest token cost
        ]
        all_models = set(budget_hierarchy) | set(premium_hierarchy)

        if selected_model and selected_model != "auto":
            candidates = [selected_model] if selected_model in all_models else premium_hierarchy
        elif os.getenv("LLM_AUTO_STRATEGY", "premium") == "budget":
            candidates = budget_hierarchy
        else:
            candidates = premium_hierarchy

        from openai import OpenAI
        import httpx

        base_url = os.getenv("CLAUDEHUB_BASE_URL") or "https://api.claudehub.fun/v1"
        client = OpenAI(
            api_key=claudehub_key,
            base_url=base_url,
            max_retries=0,
            timeout=httpx.Timeout(55.0, connect=6.0, read=55.0),
        )

        for m_name in candidates:
            # 1. Send ultra-light 1-token probe
            print(f"[LLM Probe] Pinging '{m_name}' (1-token check, ~0 tokens burned)...", flush=True)
            if not ping_model(client, m_name):
                print(f"[LLM Probe] '{m_name}' did not answer in 5.0s -> skipped (0 tokens spent on heavy prompt), cascading down...", flush=True)
                continue

            # 2. Model answered! Send full generation prompt
            try:
                print(f"[LLM] '{m_name}' is ALIVE! Generating full basketball plan...", flush=True)
                response = client.chat.completions.create(
                    model=m_name,
                    messages=[
                        {"role": "system", "content": full_system_instruction},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=2200,
                )
                content = response.choices[0].message.content
                print(f"[LLM] ClaudeHub '{m_name}' SUCCEEDED ({len(content)} chars)", flush=True)
                parsed_json = extract_and_parse_json(content)
                usage = getattr(response, "usage", None)
                usage_info = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                }
                return {"source": f"claudehub-api ({m_name})", "data": parsed_json, "_usage": usage_info}
            except Exception as e:
                print(f"[LLM] Generation on '{m_name}' failed ({type(e).__name__}: {e}) -> falling down cascade...", flush=True)

    # Option 2: Gemini API
    if gemini_key and gemini_key != "your_gemini_api_key_here":
        model_name = os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
        try:
            import google.generativeai as genai

            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=full_system_instruction,
            )
            response = model.generate_content(
                user_prompt,
                generation_config={"response_mime_type": "application/json"},
            )
            parsed_json = extract_and_parse_json(response.text)
            usage_meta = getattr(response, "usage_metadata", None)
            usage_info = {
                "prompt_tokens": getattr(usage_meta, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(usage_meta, "candidates_token_count", 0) or 0,
            }
            return {"source": f"gemini-api ({model_name})", "data": parsed_json, "_usage": usage_info}
        except Exception as e:
            print(f"Gemini API call failed ({model_name}): {type(e).__name__}: {e}")

    # Option 3: OpenAI API
    if openai_key and openai_key != "your_openai_api_key_here":
        model_name = os.getenv("AI_MODEL_NAME") or "gpt-4o-mini"
        try:
            from openai import OpenAI

            client = OpenAI(api_key=openai_key)

            response = client.chat.completions.create(
                model=model_name,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": full_system_instruction},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = response.choices[0].message.content
            parsed_json = extract_and_parse_json(content)
            usage = getattr(response, "usage", None)
            usage_info = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }
            return {"source": f"openai-api ({model_name})", "data": parsed_json, "_usage": usage_info}
        except Exception as e:
            print(f"OpenAI API call failed ({model_name}): {type(e).__name__}: {e}")

    # Fallback mode
    return {
        "source": "stub_mock (Check API.env keys)",
        "data": {
            "schedule": [
                {
                    "day": 1,
                    "focus": "High-Intensity Ball Handling & Plyometrics",
                    "exercises": [
                        {"name": "Heavy Ball Pound Dribble", "sets": 3, "duration": "45 sec"},
                        {"name": "Depth Jumps to Rim", "sets": 4, "reps": "6 jumps"},
                    ],
                },
            ]
        },
    }
