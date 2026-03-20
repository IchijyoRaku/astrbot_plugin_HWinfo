import json
import re
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

CPU_SINGLE_PATH = Path("data/cpu/r23single.json")
GPU_PATH = Path("data/gpu/timespy.json")
GPU_RANK_IMAGE = Path("data/gpu/显卡天梯图.png")
PLUGIN_VERSION = "0.1"


@register("astrbot_plugin_HWinfo", "IchijyoRaku", "硬件信息与性能对比插件", PLUGIN_VERSION)
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

    def _compact(self, text: str) -> str:
        return self._normalize_text(text).replace(" ", "")

    def _display_name(self, item: dict[str, Any]) -> str:
        parts = [item.get("vendor"), item.get("series"), item.get("name")]
        return " ".join(str(part) for part in parts if part and part != "Unknown")

    def _extract_number_tokens(self, text: str) -> list[str]:
        normalized = self._normalize_text(text)
        return re.findall(r"\d{3,5}[a-z]{0,3}", normalized)

    def _extract_text_tokens(self, text: str) -> list[str]:
        normalized = self._normalize_text(text)
        return re.findall(r"[a-z]+|\d{3,5}[a-z]*", normalized)

    def _item_aliases(self, item: dict[str, Any]) -> list[str]:
        vendor = str(item.get("vendor", ""))
        series = str(item.get("series", ""))
        name = str(item.get("name", ""))
        aliases = {
            self._display_name(item),
            f"{series} {name}".strip(),
            f"{vendor} {series} {name}".strip(),
            name,
            self._compact(name),
            self._compact(f"{series} {name}"),
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

    def _score_candidate(self, query: str, item: dict[str, Any]) -> float:
        query_compact = self._compact(query)
        query_numbers = self._extract_number_tokens(query)
        query_tokens = [
            token for token in self._extract_text_tokens(query)
            if token not in {"cpu", "gpu", "laptop", "desktop", "相当于", "什么", "型号", "查询"}
        ]
        if not query_compact:
            return 0.0

        aliases = self._item_aliases(item)
        best_score = 0.0
        normalized_query = self._normalize_text(query)
        for alias in aliases:
            alias_compact = self._compact(alias)
            alias_tokens = self._extract_text_tokens(alias)
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
        threshold = max(60, best_score * 0.6)
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

    def _resolve_image_path(self, path: Path | str) -> str:
        return str((Path(__file__).parent / Path(path)).resolve())

    def _image_chain(self, path: Path | str):
        return [Comp.Image.fromFileSystem(self._resolve_image_path(path))]

    async def _send_rank_image(self, event: AstrMessageEvent):
        yield event.chain_result(self._image_chain(GPU_RANK_IMAGE))

    async def _handle_search(self, event: AstrMessageEvent, category: str, query: str, items: list[dict[str, Any]]):
        query = query.strip()
        if not query:
            if category == "gpu":
                async for result in self._send_rank_image(event):
                    yield result
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
        for token in ["显卡", "gpu", "相当于", "比较", "对比", "什么", "型号", "对应", "接近", "大概", "约等于", "性能"]:
            lowered = lowered.replace(token, " ")
        lowered = re.sub(r"\s+", " ", lowered).strip()
        return source_type, lowered

    def _gpu_series_rank(self, item: dict[str, Any]) -> int | None:
        match = re.search(r"(\d{4})", str(item.get("name", "")))
        if not match:
            return None
        value = int(match.group(1))
        return value // 1000

    def _same_series_candidates(self, candidates: list[dict[str, Any]], base_series: int) -> list[dict[str, Any]]:
        return [item for item in candidates if self._gpu_series_rank(item) == base_series]

    def _pick_generation_equivalent(self, base_item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        base_score = base_item["score"]
        base_series = self._gpu_series_rank(base_item)
        if base_series is None:
            return min(candidates, key=lambda item: abs(item["score"] - base_score))

        ordered_series = [series for series in range(base_series, 1, -1)]
        for series in ordered_series:
            series_candidates = self._same_series_candidates(candidates, series)
            if not series_candidates:
                continue
            best = min(series_candidates, key=lambda item: abs(item["score"] - base_score))
            diff_ratio = abs(best["score"] - base_score) / max(base_score, 1)
            if diff_ratio <= 0.10:
                return best
        return min(candidates, key=lambda item: abs(item["score"] - base_score))

    def _find_equivalent_gpus(self, base_item: dict[str, Any], target_type: str) -> list[dict[str, Any]]:
        candidates = [
            item for item in self.gpu_items
            if item.get("type") == target_type and item.get("generation")
        ]
        match = self._pick_generation_equivalent(base_item, candidates)
        return [match] if match else []

    def _format_compare_result(self, base_item: dict[str, Any], target_items: list[dict[str, Any]]) -> str:
        target_item = target_items[0]
        diff_ratio = (target_item["score"] - base_item["score"]) / max(base_item["score"], 1)
        diff_percent = f"{diff_ratio * 100:+.1f}%"
        return "\n".join(
            [
                "显卡性能对比",
                f"基准型号：{self._display_name(base_item)}",
                f"对比型号：{self._display_name(target_item)}",
                f"基准跑分：{base_item['score']}",
                f"对比跑分：{target_item['score']}",
                f"性能差距：{diff_percent}",
            ]
        )

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
        """发送显卡天梯图。"""
        async for result in self._send_rank_image(event):
            yield result
