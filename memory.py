import json
import re
import time
from pathlib import Path
from collections import Counter

MEMORY_FILE = Path(__file__).resolve().parent / "memories.json"

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "my", "your", "his", "her",
    "its", "our", "their", "to", "of", "in", "on", "at", "for", "with",
    "and", "or", "but", "that", "this", "these", "those", "do", "does",
    "did", "have", "has", "had", "will", "would", "should", "could", "can",
    "me", "what", "which", "who", "whom",
}


def _load():
    if MEMORY_FILE.exists():
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    return []


def _save(memories):
    MEMORY_FILE.write_text(json.dumps(memories, indent=2), encoding="utf-8")


def _tokenize(text):
    words = re.findall(r"[a-z0-9']+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


def remember_fact(fact):
    memories = _load()
    normalized = fact.strip().lower()
    for m in memories:
        if m["fact"].strip().lower() == normalized:
            return f"Already remembered: {fact}"
    memories.append({"id": f"mem_{int(time.time() * 1000)}", "fact": fact.strip()})
    _save(memories)
    return f"Remembered: {fact}"


def recall_memories(query, n_results=3):
    """Returns a plain list of the most relevant remembered facts, or []."""
    memories = _load()
    if not memories:
        return []

    query_words = set(_tokenize(query))
    if not query_words:
        return []

    scored = []
    for m in memories:
        fact_words = _tokenize(m["fact"])
        if not fact_words:
            continue
        overlap = len(query_words & set(fact_words))
        if overlap > 0:
            scored.append((overlap, m["fact"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [fact for _, fact in scored[:n_results]]


def recall_memories_text(query, n_results=5):
    """Same as recall_memories but formatted for the model to read when
    called explicitly as a tool."""
    docs = recall_memories(query, n_results)
    if not docs:
        return "No relevant memories found."
    return "\n".join(f"- {d}" for d in docs)


def list_all_memories():
    memories = _load()
    if not memories:
        return "No memories stored yet."
    return "\n".join(f"- {m['fact']}" for m in memories)


def forget_fact(fact_substring):
    """Deletes any stored memory containing the given text (case-insensitive)."""
    memories = _load()
    needle = fact_substring.lower()
    remaining = [m for m in memories if needle not in m["fact"].lower()]
    deleted_count = len(memories) - len(remaining)
    if deleted_count == 0:
        return f"No memory found matching: {fact_substring}"
    _save(remaining)
    return f"Forgot {deleted_count} matching memory/memories."