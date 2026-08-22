import json
import os
import re
import time
from array import array
from bisect import bisect_left
from collections import Counter
from typing import Dict, List, Tuple
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

# In-memory caches: page index and the inverted term index built from it
_FULL_PAGE_INDEX: List[Dict] = []
_TERM_POSTINGS: Dict[str, array] = {}   # term -> flat [entry_idx, count, ...] pairs
_SORTED_VOCAB: List[str] = []           # sorted terms, for prefix expansion via bisect


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


def _build_term_index() -> None:
    """
    Builds an inverted index (term -> [(page, count)]) once at first search.
    Replaces a per-request linear scan with lowercase + substring counting
    over ~9 MB of text, which dominated request latency.
    """
    global _TERM_POSTINGS, _SORTED_VOCAB

    index = load_or_build_index()
    started = time.time()
    postings: Dict[str, Dict[int, int]] = {}

    for entry_idx, entry in enumerate(index):
        word_counts = Counter(re.findall(r"\w+", entry["text"].lower()))
        for term, cnt in word_counts.items():
            postings.setdefault(term, {})[entry_idx] = cnt

    # array('i') keeps memory compact: 2 ints per (page, count) pair
    _TERM_POSTINGS = {
        term: array("i", [v for pair in pages.items() for v in pair])
        for term, pages in postings.items()
    }
    _SORTED_VOCAB = sorted(_TERM_POSTINGS)
    print(
        f"[RAG] Inverted index built: {len(_SORTED_VOCAB)} terms over {len(index)} pages "
        f"in {time.time() - started:.1f}s",
        flush=True
    )


def _term_postings(term: str) -> Tuple[int, ...]:
    """Returns flat (page_idx, count, ...) pairs for the exact term."""
    arr = _TERM_POSTINGS.get(term)
    return tuple(arr) if arr else ()


def _prefix_postings(term: str) -> Tuple[int, ...]:
    """
    Collects postings of all vocabulary terms starting with the given prefix
    (the exact term included). Preserves most of the old substring-match
    recall (jump -> jumps/jumping) at a fraction of the cost.
    """
    pages: Dict[int, int] = {}
    i = bisect_left(_SORTED_VOCAB, term)
    while i < len(_SORTED_VOCAB) and _SORTED_VOCAB[i].startswith(term):
        arr = _TERM_POSTINGS[_SORTED_VOCAB[i]]
        for j in range(0, len(arr), 2):
            pages[arr[j]] = pages.get(arr[j], 0) + arr[j + 1]
        i += 1
    flat: List[int] = []
    for pair in pages.items():
        flat.extend(pair)
    return tuple(flat)


def get_relevant_knowledge(goal: str, injuries: str = "", position: str = "") -> str:
    """
    Searches the inverted term index of the knowledge base.
    Returns the most relevant pages as context snippets.
    """
    index = load_or_build_index()
    if not index:
        return "База знаний доступна по 8 книгам."

    if not _TERM_POSTINGS:
        _build_term_index()

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

    # Accumulate weighted scores per page via postings lists
    scores: Dict[int, int] = {}
    for term in search_terms:
        weight = 3 if len(term) > 4 else 1
        # Prefix expansion for terms long enough to be meaningful, exact match otherwise
        postings = _prefix_postings(term) if len(term) >= 3 else _term_postings(term)
        for k in range(0, len(postings), 2):
            page_idx = postings[k]
            scores[page_idx] = scores.get(page_idx, 0) + postings[k + 1] * weight

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Take TOP 4 most relevant pages
    snippets = []
    for page_idx, _score in ranked[:4]:
        entry = index[page_idx]
        header = f"=== [{entry['book']}] ==="
        snippets.append(f"{header}\n{entry['text'][:900].strip()}")

    full_context = "\n\n".join(snippets)
    # Return up to 4,000 characters of concentrated scientific context (fast & token-efficient)
    return full_context[:4000]

