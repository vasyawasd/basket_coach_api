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

import threading

# In-memory caches: page index and the inverted term index built from it
_FULL_PAGE_INDEX: List[Dict] = []
_TERM_POSTINGS: Dict[str, array] = {}   # term -> flat [entry_idx, count, ...] pairs
_SORTED_VOCAB: List[str] = []           # sorted terms, for prefix expansion via bisect
_INDEX_LOCK = threading.Lock()


def sanitize_input(text: str) -> str:
    """Sanitizes user input string to prevent control character and Unicode injection."""
    if not text:
        return ""
    # Strip dangerous ASCII control chars
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    # Strip invisible/zero-width and direction override Unicode characters
    clean = re.sub(r"[\u200B-\u200D\uFEFF\u202A-\u202E]", "", clean)
    return clean.strip()


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


# Synonyms and domain dictionary for cross-language matching (Russian <-> English) across all 16 books
SEARCH_DICTIONARY = {
    # Базовые двигательные качества и биомеханика
    "прыж": ["jump", "vertical", "plyometric", "plyo", "explosive", "bounce", "power", "countermovement", "cmj"],
    "взрыв": ["explosive", "rate of force", "rfd", "power", "eccentric", "concentric", "velocity"],
    "сил": ["strength", "squat", "deadlift", "load", "1rm", "hypertrophy", "power", "hsr"],
    "вынослив": ["endurance", "conditioning", "hiit", "aerobic", "stamina", "repeat sprint"],
    "дриблинг": ["dribble", "dribbling", "ball handling", "control", "crossover", "skills"],
    "бросок": ["shooting", "jumper", "form", "mechanics", "release", "field goal"],
    "защит": ["defense", "defensive", "slide", "agility", "lateral", "shuttle"],
    
    # Колено, сухожилия и связочный аппарат
    "колен": ["knee", "patellar", "tendon", "tendinopathy", "quadriceps", "vmo", "spanish squat", "retro", "breda"],
    "тендинопати": ["tendinopathy", "tendon", "continuum", "isometric", "eccentric", "hsr", "retro", "load management"],
    "сухожил": ["tendon", "tendinopathy", "retro", "breda", "hsr", "isometric", "malliaras", "patellar"],
    "сустав": ["joint", "rehab", "mobility", "ankle", "hip", "stiffness", "joint by joint"],
    "связк": ["ligament", "acl", "atfl", "cfl", "graft", "laxity", "valgus", "пкс"],
    "крестообразн": ["acl", "пкс", "anterior cruciate ligament", "hewett", "valgus", "sportsmetrics"],
    "пкс": ["acl", "anterior cruciate ligament", "hewett", "valgus", "ligament dominance", "крестообразн", "sportsmetrics"],
    "acl": ["acl", "пкс", "anterior cruciate ligament", "hewett", "valgus", "sportsmetrics", "rotational"],
    "вальгус": ["valgus", "dynamic valgus", "knee collapse", "acl", "trunk dominance", "hip abductor"],
    
    # Торможение, децелерация и COD (Блок 9)
    "тормож": ["deceleration", "braking", "cod", "change of direction", "stopping", "plant step", "pfc", "penultimate"],
    "децелер": ["deceleration", "braking", "eccentric", "cod", "pfc", "harper", "flywheel", "stopping"],
    "смен": ["change of direction", "cod", "agility", "cutting", "505", "deficits", "pfc", "plant step"],
    "cod": ["cod", "deceleration", "change of direction", "cutting", "nimphius", "harper", "505", "pfc", "торможение", "децелерация"],
    "5-0-5": ["505", "cod", "change of direction", "agility", "deficit", "deceleration"],
    
    # Возврат в спорт (RTS) и шкала контактов (Блок 10)
    "rts": ["rts", "return to sport", "допуск", "критерии допуска", "bern", "hop test", "lsi", "clearance"],
    "возврат": ["return to sport", "rts", "clearance", "hop test", "lsi", "kyritsis", "допуск"],
    "допуск": ["clearance", "return to sport", "rts", "lsi", "hop test", "criteria"],
    "плиометр": ["plyometric", "plyo", "stretch shortening cycle", "ssc", "depth jump", "pogo", "drop jump", "contacts"],
    "retro": ["retro", "breda", "tendon", "hsr", "heavy slow resistance", "isometric"],
    
    # Инструментальный мониторинг, тензоплатформы и VBT (Блок 11)
    "vbt": ["vbt", "velocity", "mean propulsive velocity", "mpv", "loss cutoff", "тензоплатформа", "скорость штанги"],
    "тензоплатформ": ["force plates", "force plate", "cmj", "rsimod", "braking rfd", "asymmetry", "dual force"],
    "force plate": ["force plates", "тензоплатформа", "cmj", "rsimod", "impulse", "braking rfd"],
    "cmj": ["cmj", "countermovement jump", "rsimod", "unweighting", "braking", "propulsion", "тензоплатформа"],
    "скорост": ["velocity", "vbt", "mpv", "speed", "acceleration", "rfd"],
    
    # Женский баскетбол и синдром RED-S (Блок 12)
    "red-s": ["reds", "red-s", "energy availability", "triad", "триада", "дефицит энергии", "leaf", "mountjoy"],
    "reds": ["reds", "red-s", "energy availability", "триада", "дефицит энергии"],
    "дефицит энерги": ["red-s", "energy availability", "amenorrhea", "bone density", "остеопения", "триада"],
    "женск": ["female", "red-s", "acl", "hewett", "sportsmetrics", "вальгус", "триада"],
    
    # Голеностоп (CAI) и пах (Doha) (Блок 13)
    "голеностоп": ["ankle", "cai", "sprain", "atfl", "dorsiflexion", "mulligan", "дорсифлексия", "таранная", "подворот"],
    "cai": ["cai", "chronic ankle instability", "голеностоп", "atfl", "cfl", "инверсия", "нестабильность"],
    "нестабильност": ["instability", "cai", "ankle", "balance", "airex", "proprioception"],
    "дорсифлекс": ["dorsiflexion", "ankle mobility", "mulligan", "glide", "колено к стене", "таранная"],
    "пах": ["groin", "doha", "adductor", "copenhagen", "приводящие", "squeeze test", "симфиз"],
    "doha": ["doha", "groin", "adductor", "copenhagen", "пах", "доха", "weir"],
    "приводящ": ["adductor", "copenhagen", "doha", "groin", "паховые"],
    "copenhagen": ["copenhagen", "adductor", "groin", "haroy", "doha", "приводящие"],
    "peace": ["peace love", "rehab", "acute injury", "отказ от нпвп", "протокол"],
    
    # Юношеский баскетбол, LTAD и арбитраж (Блок 14)
    "ltad": ["ltad", "long term athlete development", "ypd", "phv", "ростовой скачок", "подростки", "юноши"],
    "ypd": ["ypd", "youth physical development", "ltad", "lloyd", "oliver", "phv"],
    "юнош": ["youth", "ltad", "ypd", "phv", "апофизит", "осгуд", "подростк"],
    "подростк": ["youth", "adolescent", "phv", "ltad", "ростовой скачок", "апофизит"],
    "осгуд": ["osgood", "osgood-schlatter", "apophysitis", "апофизит", "бугристость", "ростовой"],
    "апофизит": ["apophysitis", "osgood", "sinding", "larsen", "traction", "рост", "запрет перегрузок"],
    "арбитраж": ["arbitration", "consensus", "hierarchy", "старшинство", "правило", "мета-анализ"],
    
    # Питание ISSN и микроцикл Гриффина (Блок 8 и План)
    "питани": ["dietary", "protein", "calories", "nutrition", "hydration", "recovery", "issn"],
    "issn": ["issn", "nutrition", "creatine", "protein", "supplements", "добавки", "спортивное питание"],
    "добавк": ["supplements", "creatine", "caffeine", "beta-alanine", "bcaa", "issn"],
    "креатин": ["creatine", "monohydrate", "loading", "performance", "issn"],
    "гриффин": ["griffin", "blake griffin", "микроцикл", "программа", "3-дневный", "комбо-форвард"],
    "микроцикл": ["microcycle", "mesocycle", "3-day", "периодизация", "план тренировок"],
    "испанск": ["spanish squat", "isometric", "analgesia", "patellar", "коленный сустав"]
}


def _build_term_index() -> None:
    """
    Builds an inverted index (term -> [(page, count)]) once at first search.
    Replaces a per-request linear scan with lowercase + substring counting
    over text, which dominated request latency.
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
        return "База знаний доступна по 16 книгам (Блоки 1–14, План Гриффина, Научные статьи)."

    if not _TERM_POSTINGS:
        with _INDEX_LOCK:
            if not _TERM_POSTINGS:
                _build_term_index()

    clean_goal = sanitize_input(goal).lower()
    clean_injuries = sanitize_input(injuries).lower()
    clean_pos = sanitize_input(position).lower()

    combined_input = f"{clean_goal} {clean_injuries} {clean_pos}"

    # Build search terms list
    search_terms = set(re.findall(r"\w+", combined_input))

    # Morphological normalization for Russian wordforms (strip typical suffixes/cases)
    for term in list(search_terms):
        if len(term) >= 5 and re.search(r"[а-яё]", term):
            stem = re.sub(r"(ая|ой|ый|ий|ое|ее|ые|ие|ах|ях|ам|ям|ов|ев|ом|ем|ами|ями|ия|ии|ей|ью|ет|ют|ут|ят)$", "", term)
            if len(stem) >= 3:
                search_terms.add(stem)

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

