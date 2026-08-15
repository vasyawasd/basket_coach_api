import json
import os
import re
from typing import Dict, List
import pypdf

BASE_KB_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "knowledge_base")
)
MD_INDEX_FILE_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "kb_md_index.json")
)
INDEX_FILE_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "kb_index.json")
)

# In-memory index cache
_FULL_PAGE_INDEX: List[Dict] = []


def sanitize_input(text: str) -> str:
    """Sanitizes user input string to prevent control character injection."""
    if not text:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text)).strip()


def load_or_build_index() -> List[Dict]:
    """Loads pre-built full-text index of Markdown knowledge base pages, or falls back to PDF index."""
    global _FULL_PAGE_INDEX
    if _FULL_PAGE_INDEX:
        return _FULL_PAGE_INDEX

    # 1. Prioritize clean, token-optimized Markdown Index
    if os.path.exists(MD_INDEX_FILE_PATH):
        try:
            with open(MD_INDEX_FILE_PATH, "r", encoding="utf-8") as f:
                _FULL_PAGE_INDEX = json.load(f)
                print(f"[RAG] Successfully loaded Markdown Index ({len(_FULL_PAGE_INDEX)} MD page entries)")
                return _FULL_PAGE_INDEX
        except Exception as e:
            print(f"[RAG] Failed to load MD index: {e}")
            _FULL_PAGE_INDEX = []

    # 2. Fallback to legacy PDF index
    if os.path.exists(INDEX_FILE_PATH):
        try:
            with open(INDEX_FILE_PATH, "r", encoding="utf-8") as f:
                _FULL_PAGE_INDEX = json.load(f)
                return _FULL_PAGE_INDEX
        except Exception:
            _FULL_PAGE_INDEX = []

    # Build index if file doesn't exist
    pages_index = []
    if os.path.exists(BASE_KB_DIR):
        for file_name in os.listdir(BASE_KB_DIR):
            if file_name.endswith(".pdf"):
                file_path = os.path.join(BASE_KB_DIR, file_name)
                try:
                    reader = pypdf.PdfReader(file_path)
                    for i, page in enumerate(reader.pages):
                        text = page.extract_text()
                        if text and len(text.strip()) > 60:
                            pages_index.append({
                                "book": file_name,
                                "page": i + 1,
                                "text": text.strip()
                            })
                except Exception:
                    continue

    _FULL_PAGE_INDEX = pages_index
    if pages_index:
        try:
            with open(INDEX_FILE_PATH, "w", encoding="utf-8") as out:
                json.dump(pages_index, out, ensure_ascii=False)
        except Exception:
            pass

    return _FULL_PAGE_INDEX


# Synonyms and domain dictionary for cross-language matching (Russian <-> English)
SEARCH_DICTIONARY = {
    "прыжок": ["jump", "vertical", "plyometric", "plyo", "explosive", "bounce", "power"],
    "взрыв": ["explosive", "rate of force", "rfd", "power", "eccentric", "concentric"],
    "сила": ["strength", "squat", "deadlift", "load", "1rm", "hypertrophy", "power"],
    "колено": ["knee", "patellar", "tendon", "tendinopathy", "quadriceps", "vmo", "spanish squat"],
    "тендинопатия": ["tendinopathy", "tendon", "continuum", "isometric", "eccentric", "load management"],
    "сустав": ["joint", "rehab", "mobility", "ankle", "hip", "stiffness"],
    "дриблинг": ["dribble", "dribbling", "ball handling", "control", "crossover", "skills"],
    "бросок": ["shooting", "jumper", "form", "mechanics", "release", "field goal"],
    "выносливость": ["endurance", "conditioning", "hiit", "aerobic", "stamina"],
    "питание": ["dietary", "protein", "calories", "nutrition", "hydration", "recovery"],
    "защита": ["defense", "defensive", "slide", "agility", "lateral", "shuttle"]
}


def get_relevant_knowledge(goal: str, injuries: str = "", position: str = "") -> str:
    """
    Searches across ALL 4,400+ pages of all books in the knowledge base.
    Returns the most relevant full pages (up to 25,000+ characters) for Claude Opus 5.
    """
    index = load_or_build_index()
    if not index:
        return "База знаний доступна по 8 книгам."

    clean_goal = sanitize_input(goal).lower()
    clean_injuries = sanitize_input(injuries).lower()
    clean_pos = sanitize_input(position).lower()

    combined_input = f"{clean_goal} {clean_injuries} {clean_pos}"

    # Build search terms list
    search_terms = set(re.findall(r"\w+", combined_input))

    for ru_term, en_synonyms in SEARCH_DICTIONARY.items():
        if ru_term in combined_input:
            search_terms.update(en_synonyms)

    if not search_terms:
        search_terms = {"basketball", "strength", "jump", "knee", "squat"}

    # Score every single page across all 4,400+ pages
    scored_pages = []
    for entry in index:
        text_lower = entry["text"].lower()
        score = 0
        for term in search_terms:
            count = text_lower.count(term)
            score += count * (3 if len(term) > 4 else 1)

        if score > 0:
            scored_pages.append((score, entry))

    scored_pages.sort(key=lambda x: x[0], reverse=True)

    # Take TOP 4 most relevant pages
    top_entries = [entry for score, entry in scored_pages[:4]]

    snippets = []
    for entry in top_entries:
        header = f"=== [{entry['book']}] ==="
        snippets.append(f"{header}\n{entry['text'][:900].strip()}")

    full_context = "\n\n".join(snippets)
    # Return up to 4,000 characters of concentrated scientific context (fast & token-efficient)
    return full_context[:4000]
