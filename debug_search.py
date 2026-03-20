import json
import re
from pathlib import Path

CPU_SINGLE_PATH = Path("data/cpu/r23single.json")
GPU_PATH = Path("data/gpu/timespy.json")


def load_items(path: Path):
    return json.loads(path.read_text(encoding="utf-8")).get("items", [])


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    replacements = {
        "geforce": "",
        "radeon": "",
        "graphics": "",
        "processor": "",
        "intel": "intel ",
        "amd": "amd ",
        "ryzen": "ryzen ",
        "core ultra": "core ultra ",
        "core": "core ",
        "ultra": "ultra ",
        "super": "super ",
        "笔记本": "laptop",
        "笔电": "laptop",
        "移动版": "laptop",
        "移动端": "laptop",
        "台式": "desktop",
        "桌面": "desktop",
        "独显": "gpu",
        "cpi": "cpu",
        "cup": "cpu",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact(text: str) -> str:
    return normalize_text(text).replace(" ", "")


def display_name(item: dict) -> str:
    parts = [item.get("vendor"), item.get("series"), item.get("name")]
    return " ".join(str(part) for part in parts if part and part != "Unknown")


def extract_number_tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    return re.findall(r"\d{3,5}[a-z]{0,3}", normalized)


def extract_text_tokens(text: str) -> list[str]:
    normalized = normalize_text(text)
    return re.findall(r"[a-z]+|\d{3,5}[a-z]*", normalized)


def item_aliases(item: dict) -> list[str]:
    vendor = str(item.get("vendor", ""))
    series = str(item.get("series", ""))
    name = str(item.get("name", ""))
    aliases = {
        display_name(item),
        f"{series} {name}".strip(),
        f"{vendor} {series} {name}".strip(),
        name,
        compact(name),
        compact(f"{series} {name}"),
    }
    if vendor.upper() == "AMD" and series.startswith("Ryzen"):
        series_level = series.replace("Ryzen", "").strip()
        aliases.add(f"ryzen {series_level} {name}".strip())
        aliases.add(f"r{series_level} {name}".strip())
        aliases.add(f"{series_level} {name}".strip())
        aliases.add(f"{series_level}{name}".strip())
        aliases.add(name.replace(" ", ""))
    if vendor.upper() == "INTEL" and series.startswith("Core"):
        compact_name = name.replace(" ", "")
        aliases.add(compact_name)
        aliases.add(f"{series} {compact_name}".strip())
        if series.startswith("Core Ultra"):
            aliases.add(f"ultra {compact_name}".strip())
        else:
            aliases.add(f"{series.replace('Core', 'i').strip()} {compact_name}".strip())
    return [alias for alias in aliases if alias]


def score_candidate(query: str, item: dict) -> float:
    query_compact = compact(query)
    query_numbers = extract_number_tokens(query)
    query_tokens = [
        token for token in extract_text_tokens(query)
        if token not in {"cpu", "gpu", "laptop", "desktop", "相当于", "什么", "型号", "查询"}
    ]
    if not query_compact:
        return 0.0

    aliases = item_aliases(item)
    best_score = 0.0
    normalized_query = normalize_text(query)
    for alias in aliases:
        alias_compact = compact(alias)
        alias_tokens = extract_text_tokens(alias)
        score = 0.0

        if query_compact == alias_compact:
            score += 260
        elif query_compact in alias_compact:
            score += 220
        elif alias_compact in query_compact:
            score += 120

        if query_numbers:
            matched_numbers = 0
            for number in query_numbers:
                if number in alias_compact:
                    score += 120
                    matched_numbers += 1
            if matched_numbers == 0:
                continue
            score += matched_numbers * 30

        for token in query_tokens:
            if token in alias_compact:
                score += 35
            elif any(token == alias_token for alias_token in alias_tokens):
                score += 25

        if normalized_query.startswith("cpu"):
            score += 5
        if "laptop" in normalized_query and item.get("type") == "laptop":
            score += 30
        if "desktop" in normalized_query and item.get("type") == "desktop":
            score += 30

        if score > best_score:
            best_score = score
    return best_score


def search_items(items: list[dict], query: str, limit: int = 8):
    scored = []
    for item in items:
        score = score_candidate(query, item)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1].get("rank", 999999), display_name(x[1])))
    if not scored:
        return []
    best_score = scored[0][0]
    threshold = max(60, best_score * 0.6)
    candidates = [(score, item) for score, item in scored if score >= threshold]
    if candidates:
        return candidates[:limit]
    query_numbers = extract_number_tokens(query)
    if query_numbers:
        fallback = [
            (score, item) for score, item in scored
            if all(number in compact(display_name(item)) for number in query_numbers)
        ]
        if fallback:
            return fallback[:limit]
    return scored[:limit]


if __name__ == "__main__":
    cpu_items = load_items(CPU_SINGLE_PATH)
    gpu_items = load_items(GPU_PATH)
    test_cases = [
        ("cpu", "9700x", cpu_items),
        ("cpu", "r7 9700x", cpu_items),
        ("cpu", "cpu 9700x", cpu_items),
        ("gpu", "5090", gpu_items),
        ("gpu", "5070", gpu_items),
    ]
    for kind, query, items in test_cases:
        print(f"===== {kind} {query} =====")
        results = search_items(items, query, limit=10)
        if not results:
            print("NO RESULT")
            continue
        for idx, (score, item) in enumerate(results, start=1):
            print(idx, score, display_name(item), item.get("type"), item.get("score"))
