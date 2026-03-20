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
    text = text.replace("笔记本", "").replace("笔电", "")
    text = text.replace("台式", "").replace("桌面", "")
    return re.sub(r"[^a-z0-9]+", "", text)


def extract_strict_model(item: dict) -> str:
    return re.sub(r"\s+", "", str(item.get("name", "")).strip()).lower()


def display_name(item: dict) -> str:
    return " ".join(str(part) for part in [item.get("vendor"), item.get("series"), item.get("name")] if part and part != "Unknown")


def strict_search(items: list[dict], query: str):
    normalized_query = normalize_query_model(query)
    result = []
    for item in items:
        model = extract_strict_model(item)
        if normalized_query in model:
            result.append(item)
    return result


if __name__ == "__main__":
    cpu_items = load_items(CPU_SINGLE_PATH)
    gpu_items = load_items(GPU_PATH)
    tests = [
        ("cpu", "9700x", cpu_items),
        ("cpu", "cpu 9700x", cpu_items),
        ("gpu", "5090", gpu_items),
        ("gpu", "5070", gpu_items),
    ]
    for kind, query, items in tests:
        print(f"===== {kind} {query} =====")
        matches = strict_search(items, query)
        if not matches:
            print("NO RESULT")
            continue
        for idx, item in enumerate(matches, start=1):
            print(idx, extract_strict_model(item), display_name(item), item.get("type"), item.get("score"))
