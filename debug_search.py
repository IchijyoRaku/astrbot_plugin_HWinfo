import json
import re
from pathlib import Path

CPU_SINGLE_PATH = Path("data/cpu/r23single.json")
GPU_PATH = Path("data/gpu/timespy.json")


def load_items(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("items", [])


def normalize_query_model(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("cpu", "").replace("gpu", "")
    text = text.replace("显卡", "").replace("处理器", "")
    text = text.replace("笔记本", "laptop").replace("笔电", "laptop")
    text = text.replace("台式", "desktop").replace("桌面", "desktop")
    return re.sub(r"[^a-z0-9]+", "", text)


def extract_model_core_and_suffix(text: str):
    normalized = normalize_query_model(text)
    match = re.search(r"(\d{3,5})([a-z0-9]*)", normalized)
    if not match:
        return normalized, ""
    return match.group(1), match.group(2)


def extract_strict_model(item: dict) -> str:
    return re.sub(r"\s+", "", str(item.get("name", "")).strip()).lower()


def display_name(item: dict) -> str:
    return " ".join(str(part) for part in [item.get("vendor"), item.get("series"), item.get("name")] if part and part != "Unknown")


def score_model_match(query: str, item: dict) -> float:
    normalized_query = normalize_query_model(query)
    model = extract_strict_model(item)
    core, suffix = extract_model_core_and_suffix(query)
    score = 0.0
    if normalized_query == model:
        score += 1000
    elif normalized_query in model:
        score += 300
    if core and core in model:
        score += 200
    else:
        return 0.0
    if suffix:
        if model.endswith(suffix):
            score += 700
        elif suffix in model:
            score += 120
        else:
            score -= 600
        score += len(suffix) * 40
    else:
        if any(token in model for token in ["ti", "super", "x3d", "mobile"]):
            score -= 120
    if "laptop" in normalized_query:
        score += 250 if item.get("type") == "laptop" else -250
    if "desktop" in normalized_query:
        score += 250 if item.get("type") == "desktop" else -250
    return score


def fuzzy_search(items: list[dict], query: str):
    scored = []
    for item in items:
        score = score_model_match(query, item)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1].get("rank", 999999), display_name(x[1])))
    if not scored:
        return []
    best_score = scored[0][0]
    query_core, query_suffix = extract_model_core_and_suffix(query)
    threshold = max(50, best_score - 300)
    filtered = [(score, item) for score, item in scored if score >= threshold]
    if query_suffix:
        precise = []
        for score, item in filtered:
            model = extract_strict_model(item)
            if query_core in model and model.endswith(query_suffix):
                precise.append((score, item))
        if precise:
            filtered = precise
    return filtered


if __name__ == "__main__":
    cpu_items = load_items(CPU_SINGLE_PATH)
    tests = [
        "9800x3d",
        "9700x",
    ]
    for query in tests:
        print(f"===== cpu {query} =====")
        matches = fuzzy_search(cpu_items, query)
        if not matches:
            print("NO RESULT")
            continue
        for idx, (score, item) in enumerate(matches, start=1):
            print(idx, score, extract_strict_model(item), display_name(item))
