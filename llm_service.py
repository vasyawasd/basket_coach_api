import json
import os
import re
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


def call_llm_api(system_prompt: str, user_prompt: str, context_text: str, selected_model: Optional[str] = None) -> Dict[str, Any]:
    """
    Calls ClaudeHub (app.claudehub.fun), Google Gemini, or OpenAI API
    to generate an exhaustive, professional training plan using the user-selected model.
    """
    claudehub_key = os.getenv("CLAUDEHUB_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    full_system_instruction = (
        f"{system_prompt}\n\n"
        f"УЧТИТЕ И ИСПОЛЬЗУЙТЕ СЛЕДУЮЩИЕ МЕТОДИЧЕСКИЕ МАТЕРИАЛЫ БАЗЫ ЗНАНИЙ:\n"
        f"{context_text}\n\n"
        f"ВАЖНО: Верните ответ ИСКЛЮЧИТЕЛЬНО в формате валидного JSON без разговорного текста вокруг."
    )

    # Option 1: ClaudeHub API (app.claudehub.fun)
    if claudehub_key and claudehub_key != "your_claudehub_api_key_here":
        model_name = selected_model or os.getenv("CLAUDEHUB_MODEL") or "claude-sonnet-5"
        try:
            from openai import OpenAI
            import httpx

            base_url = os.getenv("CLAUDEHUB_BASE_URL") or "https://api.claudehub.fun/v1"
            # ponytail: no streaming — upstream often holds connection open without sending data,
            # causing infinite hangs. Sync request lets httpx timeout work properly.
            client = OpenAI(
                api_key=claudehub_key,
                base_url=base_url,
                timeout=httpx.Timeout(120.0, connect=15.0, read=120.0),
            )
            print(f"[LLM] Calling ClaudeHub ({model_name}), prompt ~{len(full_system_instruction)} chars...")
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": full_system_instruction},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=6000,
            )
            content = response.choices[0].message.content

            print(f"[LLM] ClaudeHub response received: {len(content)} chars")
            parsed_json = extract_and_parse_json(content)
            return {"source": f"claudehub-api ({model_name})", "data": parsed_json}
        except Exception as e:
            print(f"[LLM] ClaudeHub API call failed ({model_name}): {type(e).__name__}: {e}")

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
            return {"source": f"gemini-api ({model_name})", "data": parsed_json}
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
            return {"source": f"openai-api ({model_name})", "data": parsed_json}
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
