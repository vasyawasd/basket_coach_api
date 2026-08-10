import sys
sys.stdout.reconfigure(encoding="utf-8")

import os
import time
from dotenv import load_dotenv

load_dotenv("API.env")

from openai import OpenAI

key = os.getenv("CLAUDEHUB_API_KEY")
print(f"Key: {key[:10]}...{key[-4:]}")

client = OpenAI(api_key=key, base_url="https://api.claudehub.fun/v1", timeout=30)

print("Testing stream=True (like your app)...")
start = time.time()
try:
    response = client.chat.completions.create(
        model="claude-opus-5",
        messages=[
            {"role": "system", "content": "Reply ONLY valid JSON: {\"ok\": true}"},
            {"role": "user", "content": "test"},
        ],
        max_tokens=50,
        stream=True,
    )
    content = ""
    for chunk in response:
        if chunk.choices and len(chunk.choices) > 0:
            delta = chunk.choices[0].delta
            if hasattr(delta, "content") and delta.content:
                content += delta.content
                print(f"  chunk: {delta.content!r}")
    elapsed = time.time() - start
    print(f"Stream result ({elapsed:.1f}s): {content}")
except Exception as e:
    elapsed = time.time() - start
    print(f"Stream FAILED ({elapsed:.1f}s): {type(e).__name__}: {e}")

print("\nTesting with FULL system prompt + RAG context (like real request)...")
from rag import get_relevant_knowledge
start = time.time()
ctx = get_relevant_knowledge(goal="vertical jump", injuries="none", position="PG")
print(f"RAG context length: {len(ctx)} chars ({time.time()-start:.1f}s)")

system_prompt = (
    "Ты — элитный баскетбольный тренер. "
    "Составь программу тренировок на русском языке. "
    "Верни результат СТРОГО в формате JSON.\n\n"
    f"МЕТОДИЧЕСКИЕ МАТЕРИАЛЫ:\n{ctx}\n\n"
    "ВАЖНО: Верните ответ ИСКЛЮЧИТЕЛЬНО в формате валидного JSON."
)

user_prompt = (
    "Player parameters:\n"
    "- Height: 190 cm\n"
    "- Weight: 85 kg\n"
    "- Position: PG\n"
    "- Goal: Vertical jump\n"
    "- Days per week: 3\n"
    "- Injuries: None"
)

print(f"System prompt length: {len(system_prompt)} chars")
print(f"User prompt length: {len(user_prompt)} chars")
print("Calling API with stream=True...")

start = time.time()
try:
    response = client.chat.completions.create(
        model="claude-opus-5",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=6000,
        stream=True,
    )
    content = ""
    chunk_count = 0
    for chunk in response:
        if chunk.choices and len(chunk.choices) > 0:
            delta = chunk.choices[0].delta
            if hasattr(delta, "content") and delta.content:
                content += delta.content
                chunk_count += 1
                if chunk_count % 20 == 0:
                    print(f"  ...received {chunk_count} chunks, {len(content)} chars so far ({time.time()-start:.1f}s)")
    elapsed = time.time() - start
    print(f"\nDONE in {elapsed:.1f}s — {chunk_count} chunks, {len(content)} chars total")
    print(f"First 200 chars: {content[:200]}")
except Exception as e:
    elapsed = time.time() - start
    print(f"FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
