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


def is_valid_plan_data(data: Any) -> bool:
    """Verifies that the returned data is a genuine basketball training plan, not an error payload."""
    if not isinstance(data, dict):
        return False
    if "error" in data:
        return False
    # Must contain either schedule or days or exercises
    return any(k in data for k in ("schedule", "days", "weekly_schedule", "exercises"))


def ping_model(client, model_name: str) -> bool:
    """
    Sends an ultra-lightweight 1-token health probe (costs ~0 tokens, 8.0s timeout).
    Returns True only if the model is alive and returns 200 OK.
    Supports reasoning models where initial output is in reasoning_content.
    """
    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=2,
            timeout=8.0,
        )
        msg = resp.choices[0].message
        content = msg.content
        reasoning = getattr(msg, "reasoning_content", None)
        return bool(content is not None or reasoning is not None)
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
            "claude-sonnet-4.6",    # 1. Ultra-fast (5.2s) & 100% Strict JSON
            "gpt-5.6-terra",        # 2. Fast GPT flagship (8.5s)
            "gemini-3.7-flash",     # 3. Super-fast frontier model (9.9s)
            "claude-haiku-4.5",     # 4. Fast reliable lightweight Claude
            "glm-5.2",              # 5. Robust multi-discipline fallback
        ]
        premium_hierarchy = [
            "claude-sonnet-4.6",    # 1. Top speed & 100% Strict JSON (5.2s)
            "claude-opus-4.8",      # 2. Elite height & biomechanics specialist (6.3s)
            "claude-opus-5",        # 3. Apex reasoning & reactive plyometrics (7.0s)
            "gpt-5.6-terra",        # 4. Premier OpenAI GPT flagship (8.5s)
            "gemini-3.7-flash",     # 5. High-speed Google frontier model (9.9s)
            "gpt-5.5",              # 6. Deep basketball game-situation specialist (17.6s)
            "glm-5.2",              # 7. Comprehensive 19-exercise backup (16.3s)
        ]
        base_hierarchy = budget_hierarchy if os.getenv("LLM_AUTO_STRATEGY", "premium") == "budget" else premium_hierarchy

        if selected_model and selected_model != "auto":
            candidates = [selected_model] + [m for m in base_hierarchy if m != selected_model]
        else:
            candidates = base_hierarchy

        from openai import OpenAI
        import httpx

        base_url = os.getenv("CLAUDEHUB_BASE_URL") or "https://api.claudehub.fun/v1"
        client = OpenAI(
            api_key=claudehub_key,
            base_url=base_url,
            max_retries=0,
            timeout=httpx.Timeout(45.0, connect=8.0, read=45.0),
        )

        for m_name in candidates:
            # 1. Send ultra-light 1-token probe
            print(f"[LLM Probe] Pinging '{m_name}' (1-token check, ~0 tokens burned)...", flush=True)
            if not ping_model(client, m_name):
                print(f"[LLM Probe] '{m_name}' did not answer in 8.0s -> skipped (0 tokens spent on heavy prompt), cascading down...", flush=True)
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
                if not is_valid_plan_data(parsed_json):
                    raise ValueError(f"Model returned invalid plan payload or API error structure: {list(parsed_json.keys())}")
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

