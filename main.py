import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

CPU_SINGLE_PATH = Path("data/cpu/r23single.json")
GPU_PATH = Path("data/gpu/timespy.json")
QUERY_STOPWORDS = {
    "cpu",
    "gpu",
    "显卡",
    "处理器",
    "查询",
    "查",
    "看",
    "参数",
    "型号",
    "什么",
    "多少",
    "对比",
    "比较",
    "相当于",
    "大概",
    "约等于",
    "对应",
    "接近",
    "的",
}


@register("astrbot_plugin_HWinfo", "IchijyoRaku", "硬件信息与性能对比插件", "1.0.0")
class HWInfoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cpu_items = self._load_items(CPU_SINGLE_PATH)
        self.gpu_items = self._load_items(GPU_PATH)
        self.pending_choices: dict[str, dict[str, Any]] = {}

    def _load_items(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            logger.warning("数据文件不存在: %s", path)
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("items", [])

    def _normalize_text(self, text: str) -> str:
        text = text.lower().strip()
        replacements = {
            "geforce": "",
            "radeon": "",
            "graphics": "",
            "processor": "",
            "intel": "i",
            "amd": "amd",
            "ryzen": "r",
            "core": "i",
            "ultra": "u",
            "super": "s",
            "ti super": "ti",
            "ti": "ti",
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
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _compact_text(self, text: str) -> str:
        return self._normalize_text(text).replace(" ", "")

    def _extract_number_tokens(self, text: str) -> list[str]:
        return re.findall(r"\d{3,5}[a-z]{0,3}", self._normalize_text(text))

    def _extract_query_tokens(self, text: str) -> list[str]:
        normalized = self._normalize_text(text)
        raw_tokens = re.findall(r"[a-z]+\d*[a-z]*|\d{3,5}[a-z]*", normalized)
        merged: list[str] = []
        i = 0
        while i < len(raw_tokens):
            token = raw_tokens[i]
            next_token = raw_tokens[i + 1] if i + 1 < len(raw_tokens) else ""
            if token in {"rtx", "gtx", "rx", "i", "r", "u"} and next_token and re.fullmatch(r"\d{3,5}[a-z]{0,3}", next_token):
                merged.append(f"{token}{next_token}")
                i += 2
                continue
            if token in QUERY_STOPWORDS:
                i += 1
                continue
            merged.append(token)
            i += 1
        ordered: list[str] = []
        for token in merged:
            if token and token not in ordered:
                ordered.append(token)
        return ordered

    def _build_search_blob(self, item: dict[str, Any]) -> dict[str, Any]:
        display_name = self._display_name(item)
        name = str(item.get("name", ""))
        vendor = str(item.get("vendor", ""))
        series = str(item.get("series", ""))
        generation = str(item.get("generation", ""))
        item_type = str(item.get("type", ""))
        full_text = " ".join(part for part in [vendor, series, name, generation, item_type, display_name] if part and part != "Unknown")
        normalized = self._normalize_text(full_text)
        compact = normalized.replace(" ", "")
        tokens = self._extract_query_tokens(full_text)
        numbers = self._extract_number_tokens(full_text)
        return {
            "display_name": display_name,
            "normalized": normalized,
            "compact": compact,
            "tokens": tokens,
            "numbers": numbers,
        }

    def _display_name(self, item: dict[str, Any]) -> str:
        parts = [item.get("vendor"), item.get("series"), item.get("name")]
        return " ".join(str(p) for p in parts if p and p != "Unknown")

    def _score_candidate(self, query: str, item: dict[str, Any]) -> float:
        blob = self._build_search_blob(item)
        query_text = self._normalize_text(query)
        query_compact = self._compact_text(query)
        query_tokens = self._extract_query_tokens(query)
        query_numbers = self._extract_number_tokens(query)
        if not query_text:
            return 0.0

        score = 0.0
        if query_compact and query_compact in blob["compact"]:
            score += 200

        if query_numbers:
            matched_numbers = 0
            for number in query_numbers:
                if any(number in candidate for candidate in blob["numbers"]):
                    score += 120
                    matched_numbers += 1
            if matched_numbers == 0:
                return 0.0
            score += matched_numbers * 20

        for token in query_tokens:
            if token in blob["tokens"]:
                score += 45
                continue
            if token in blob["compact"]:
                score += 30
                continue
            best_ratio = 0.0
            for candidate in blob["tokens"]:
                ratio = SequenceMatcher(None, token, candidate).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
            if best_ratio >= 0.88:
                score += 24
            elif best_ratio >= 0.75:
                score += 12

        overall_ratio = SequenceMatcher(None, query_compact, blob["compact"]).ratio() if query_compact else 0.0
        score += overall_ratio * 20

        if "laptop" in query_text and item.get("type") == "laptop":
            score += 35
        if "desktop" in query_text and item.get("type") == "desktop":
            score += 35
        return score

    def _search_items(self, items: list[dict[str, Any]], query: str, limit: int = 8) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            score = self._score_candidate(query, item)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: (-x[0], x[1].get("rank", 999999), self._display_name(x[1])))
        if not scored:
            return []
        best_score = scored[0][0]
        threshold = max(25, best_score * 0.65)
        return [item for score, item in scored if score >= threshold][:limit]

    def _format_item_detail(self, category: str, item: dict[str, Any]) -> str:
        rows = []
        for label, key in [
            ("厂商", "vendor"),
            ("系列", "series"),
            ("型号", "name"),
            ("类型", "type"),
            ("代际", "generation"),
            ("显存容量", "memory_size"),
            ("显存类型", "memory_type"),
            ("发布日期", "release_date"),
            ("跑分", "score"),
            ("核心数", "cores"),
            ("线程数", "threads"),
            ("P 核", "p_cores"),
            ("E 核", "e_cores"),
            ("基准频率", "base_clock"),
        ]:
            value = item.get(key)
            if value is None or value == "Unknown":
                continue
            rows.append(f"{label}：{value}")
        return f"{category.upper()} 详细信息\n" + "\n".join(rows)

    def _format_rank_summary(self) -> str:
        top_items = sorted(
            [item for item in self.gpu_items if item.get("score")],
            key=lambda item: item["score"],
            reverse=True,
        )[:10]
        lines = ["显卡性能榜单（前 10）"]
        for idx, item in enumerate(top_items, start=1):
            lines.append(f"{idx}. {self._display_name(item)}｜{item.get('score', '-')}")
        lines.append("提示：发送 gpu 型号 可查询具体参数。")
        return "\n".join(lines)

    async def _handle_search(self, event: AstrMessageEvent, category: str, query: str, items: list[dict[str, Any]]):
        query = query.strip()
        if not query:
            if category == "gpu":
                yield event.plain_result(self._format_rank_summary())
            else:
                yield event.plain_result(f"请输入要查询的{category.upper()} 型号。")
            return

        matches = self._search_items(items, query)
        if not matches:
            yield event.plain_result(f"未找到和 {query} 相关的{category.upper()} 型号。")
            return

        if len(matches) == 1:
            yield event.plain_result(self._format_item_detail(category, matches[0]))
            return

        user_id = str(event.get_sender_id())
        self.pending_choices[user_id] = {"category": category, "items": matches}
        lines = [f"找到多个{category.upper()} 候选，请回复序号选择："]
        for idx, item in enumerate(matches, start=1):
            lines.append(f"{idx}. {self._display_name(item)}")
        yield event.plain_result("\n".join(lines))

        @session_waiter(timeout=60, record_history_chains=False)
        async def choose_waiter(controller: SessionController, next_event: AstrMessageEvent):
            if str(next_event.get_sender_id()) != user_id:
                return
            text = next_event.message_str.strip()
            if text.lower() in {"取消", "退出"}:
                await next_event.send(next_event.plain_result("已取消选择。"))
                self.pending_choices.pop(user_id, None)
                controller.stop()
                return
            if not text.isdigit():
                await next_event.send(next_event.plain_result("请输入数字序号。"))
                return
            index = int(text) - 1
            candidates = self.pending_choices.get(user_id, {}).get("items", [])
            if index < 0 or index >= len(candidates):
                await next_event.send(next_event.plain_result("序号超出范围，请重新输入。"))
                return
            item = candidates[index]
            await next_event.send(next_event.plain_result(self._format_item_detail(category, item)))
            self.pending_choices.pop(user_id, None)
            controller.stop()

        try:
            await choose_waiter(event)
        except TimeoutError:
            self.pending_choices.pop(user_id, None)
            yield event.plain_result("选择已超时，请重新查询。")
        finally:
            event.stop_event()

    def _extract_type_and_query(self, text: str) -> tuple[str, str]:
        lowered = self._normalize_text(text)
        source_type = "laptop" if "laptop" in lowered else "desktop"
        for token in ["显卡", "gpu", "相当于", "比较", "对比", "什么", "型号", "对应", "接近", "大概", "约等于"]:
            lowered = lowered.replace(token, " ")
        lowered = re.sub(r"\s+", " ", lowered).strip()
        return source_type, lowered

    def _extract_gpu_rank(self, item: dict[str, Any]) -> int | None:
        match = re.search(r"(\d{3,4})", str(item.get("name", "")))
        if not match:
            return None
        return int(match.group(1))

    def _gpu_generation_priority(self, item: dict[str, Any]) -> int:
        rank = self._extract_gpu_rank(item)
        if rank is None:
            return -1
        return rank // 1000 if rank >= 1000 else rank // 100

    def _prefer_same_vendor(self, base_item: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        same_vendor = [item for item in candidates if item.get("vendor") == base_item.get("vendor")]
        return same_vendor or candidates

    def _pick_generation_equivalent(self, base_item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        preferred = self._prefer_same_vendor(base_item, candidates)
        base_score = base_item["score"]
        base_generation = self._gpu_generation_priority(base_item)
        grouped: dict[int, list[dict[str, Any]]] = {}
        for item in preferred:
            generation = self._gpu_generation_priority(item)
            if generation < 0:
                continue
            grouped.setdefault(generation, []).append(item)

        ordered_generations: list[int] = []
        if base_generation >= 0:
            ordered_generations.append(base_generation)
        ordered_generations.extend(g for g in sorted(grouped.keys(), reverse=True) if g not in ordered_generations)

        for generation in ordered_generations:
            generation_items = grouped.get(generation, [])
            if not generation_items:
                continue
            best = min(generation_items, key=lambda item: abs(item["score"] - base_score))
            diff_ratio = abs(best["score"] - base_score) / max(base_score, 1)
            if diff_ratio <= 0.10:
                return best

        return min(preferred, key=lambda item: abs(item["score"] - base_score))

    def _find_equivalent_gpus(self, base_item: dict[str, Any], target_type: str) -> list[dict[str, Any]]:
        candidates = [
            item for item in self.gpu_items
            if item.get("type") == target_type and item.get("generation")
        ]
        match = self._pick_generation_equivalent(base_item, candidates)
        return [match] if match else []

    def _format_compare_result(self, base_item: dict[str, Any], target_items: list[dict[str, Any]]) -> str:
        base_name = self._display_name(base_item)
        target_item = target_items[0]
        diff_ratio = (target_item["score"] - base_item["score"]) / max(base_item["score"], 1)
        diff_percent = f"{diff_ratio * 100:+.1f}%"
        lines = [
            "显卡性能对比",
            f"结论：{base_name} ≈ {self._display_name(target_item)}",
            f"基准跑分：{base_item['score']}",
            f"对比跑分：{target_item['score']}",
            f"性能差距：{diff_percent}",
            f"基准类型：{base_item.get('type', '-')}",
            f"对比类型：{target_item.get('type', '-')}",
        ]
        return "\n".join(lines)

    @filter.command("cpu")
    async def cpu_search(self, event: AstrMessageEvent, query: str = ""):
        """查询 CPU 型号详细信息，支持模糊搜索。"""
        async for result in self._handle_search(event, "cpu", query, self.cpu_items):
            yield result

    @filter.command("gpu")
    async def gpu_search(self, event: AstrMessageEvent, query: str = ""):
        """查询 GPU 型号详细信息，支持模糊搜索。"""
        async for result in self._handle_search(event, "gpu", query, self.gpu_items):
            yield result

    @filter.regex(r"^(?!\s*gpu\b)(?!\s*显卡天梯图\s*$).*(相当于.*显卡|显卡.*相当于|笔电.*台式|台式.*笔电).*$")
    async def compare_gpu(self, event: AstrMessageEvent):
        """比较笔电/台式显卡性能接近型号。"""
        text = event.message_str
        source_type, query = self._extract_type_and_query(text)
        target_type = "desktop" if source_type == "laptop" else "laptop"
        matches = [item for item in self._search_items(self.gpu_items, query, limit=20) if item.get("type") == source_type]
        if not matches:
            query_numbers = self._extract_number_tokens(query)
            if query_numbers:
                matches = [
                    item for item in self.gpu_items
                    if item.get("type") == source_type and any(number in self._build_search_blob(item)["compact"] for number in query_numbers)
                ]
        if not matches:
            yield event.plain_result("未找到比较型号。")
            return

        base_item = matches[0]
        target_items = self._find_equivalent_gpus(base_item, target_type)
        if not target_items:
            yield event.plain_result("未找到可比较的目标显卡。")
            return

        yield event.plain_result(self._format_compare_result(base_item, target_items))

    @filter.command("显卡天梯图")
    async def gpu_rank(self, event: AstrMessageEvent):
        """发送显卡榜单摘要。"""
        yield event.plain_result(self._format_rank_summary())
