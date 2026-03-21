import json
import re
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

BASE_DIR = Path(__file__).resolve().parent
CPU_SINGLE_PATH = BASE_DIR / "data/cpu/r23single.json"
GPU_PATH = BASE_DIR / "data/gpu/timespy.json"
GPU_RANK_IMAGE = BASE_DIR / "data/gpu/显卡天梯图.png"
PLUGIN_VERSION = "0.1"


@register("astrbot_plugin_HWinfo", "IchijyoRaku", "硬件信息与性能对比插件", PLUGIN_VERSION)
class HWInfoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cpu_items = self._load_items(CPU_SINGLE_PATH)
        self.cpu_multi_items = self._load_items(BASE_DIR / "data/cpu/r23multi.json")
        self.gpu_items = self._load_items(GPU_PATH)
        self.pending_choices: dict[str, dict[str, Any]] = {}
        logger.info("[%s] 插件初始化完成，CPU(single)=%s，CPU(multi)=%s，GPU=%s", PLUGIN_VERSION, len(self.cpu_items), len(self.cpu_multi_items), len(self.gpu_items))

    def _load_items(self, path: Path) -> list[dict[str, Any]]:
        resolved_path = path.resolve()
        logger.info("开始读取数据文件: %s", resolved_path)
        logger.info("数据文件存在性检查: %s -> %s", resolved_path, resolved_path.exists())
        if not resolved_path.exists():
            logger.warning("数据文件不存在: %s", resolved_path)
            return []
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
        items = data.get("items", [])
        logger.info("数据文件读取完成: %s，version=%s，count=%s", resolved_path, data.get("version"), len(items))
        return items

    def _resolve_image_path(self, path: Path | str) -> str:
        return str(Path(path).resolve())

    def _image_chain(self, path: Path | str):
        return [Comp.Image.fromFileSystem(self._resolve_image_path(path))]

    async def _send_rank_image(self, event: AstrMessageEvent):
        logger.info("触发显卡天梯图发送")
        yield event.chain_result(self._image_chain(GPU_RANK_IMAGE))

    def _display_name(self, item: dict[str, Any]) -> str:
        vendor = item.get("vendor")
        series = item.get("series")
        name = item.get("name")
        if vendor in {"AMD", "Apple", "INTEL"}:
            model = " ".join(str(part) for part in [series, name] if part and part != "Unknown")
            return " ".join(str(part) for part in [vendor, model] if part and part != "Unknown")
        parts = [vendor, series, name]
        return " ".join(str(part) for part in parts if part and part != "Unknown")

    def _normalize_query_model(self, text: str) -> str:
        text = text.lower().strip()
        text = text.replace("cpu", "").replace("gpu", "")
        text = text.replace("显卡", "").replace("处理器", "")
        text = text.replace("笔记本", "laptop").replace("笔电", "laptop")
        text = text.replace("台式", "desktop").replace("桌面", "desktop")
        text = re.sub(r"[^a-z0-9]+", "", text)
        if re.search(r"\d{3,5}m$", text):
            text = text[:-1] + "mobile"
        return text

    def _extract_model_core_and_suffix(self, text: str) -> tuple[str, str]:
        normalized = self._normalize_query_model(text)
        match = re.search(r"(\d{3,5})([a-z0-9]*)", normalized)
        if not match:
            return normalized, ""
        return match.group(1), match.group(2)

    def _extract_strict_model(self, item: dict[str, Any]) -> str:
        name = str(item.get("name", "")).strip()
        return re.sub(r"\s+", "", name).lower()

    def _score_model_match(self, query: str, item: dict[str, Any]) -> float:
        normalized_query = self._normalize_query_model(query)
        model = self._extract_strict_model(item)
        core, suffix = self._extract_model_core_and_suffix(query)
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

        normalized_full_query = self._normalize_query_model(query)
        if "laptop" in normalized_full_query:
            if item.get("type") == "laptop":
                score += 250
            else:
                score -= 250
        if "desktop" in normalized_full_query:
            if item.get("type") == "desktop":
                score += 250
            else:
                score -= 250

        logger.info("候选评分: query=%s item=%s model=%s score=%.2f", query, self._display_name(item), model, score)
        return score

    def _fuzzy_search_items(self, items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        logger.info("开始模糊匹配，原始输入=%s，归一化后=%s", query, self._normalize_query_model(query))
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            score = self._score_model_match(query, item)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: (-x[0], x[1].get("rank", 999999), self._display_name(x[1])))
        logger.info("模糊匹配完成，命中数量=%s", len(scored))
        for score, item in scored[:20]:
            logger.info("命中候选: %s score=%.2f", self._display_name(item), score)
        if not scored:
            return []
        best_score = scored[0][0]
        best_model = self._extract_strict_model(scored[0][1])
        query_core, query_suffix = self._extract_model_core_and_suffix(query)
        threshold = max(50, best_score - 300)
        filtered = [item for score, item in scored if score >= threshold]
        if query_suffix:
            precise = []
            for item in filtered:
                model = self._extract_strict_model(item)
                if query_core in model and model.endswith(query_suffix):
                    precise.append(item)
            if precise:
                logger.info("检测到更精确后缀匹配，优先保留 suffix=%s，数量=%s", query_suffix, len(precise))
                filtered = precise
        logger.info("最佳候选模型=%s，筛选阈值=%s，筛选后数量=%s", best_model, threshold, len(filtered))
        return filtered

    def _find_cpu_multi_score(self, item: dict[str, Any]) -> Any:
        display_name = self._display_name(item)
        for cpu_item in self.cpu_multi_items:
            if self._display_name(cpu_item) == display_name:
                return cpu_item.get("score")
        return None

    def _format_item_detail(self, category: str, item: dict[str, Any]) -> str:
        rows = []
        vendor = str(item.get("vendor", ""))
        if category == "cpu":
            field_pairs = [
                ("厂商", "vendor"),
                ("型号", None),
                ("R23单核", "score"),
                ("R23多核", None),
                ("核心数", "cores"),
                ("线程数", "threads"),
                ("基准频率", "base_clock"),
            ]
            show_hybrid = vendor not in {"AMD", "Apple"}
            if show_hybrid:
                field_pairs.extend([
                    ("P 核", "p_cores"),
                    ("E 核", "e_cores"),
                ])
        else:
            field_pairs = [
                ("厂商", "vendor"),
                ("系列", "series"),
                ("型号", "name"),
                ("类型", "type"),
                ("代际", "generation"),
                ("显存容量", "memory_size"),
                ("显存类型", "memory_type"),
                ("跑分", "score"),
            ]
        for label, key in field_pairs:
            if label == "型号" and category == "cpu":
                rows.append(f"型号：{self._display_name(item).replace(vendor + ' ', '', 1) if vendor else self._display_name(item)}")
                continue
            if label == "R23多核" and category == "cpu":
                multi_score = self._find_cpu_multi_score(item)
                if multi_score is not None:
                    rows.append(f"R23多核：{multi_score}")
                continue
            value = item.get(key) if key else None
            if value is None or value == "Unknown":
                continue
            rows.append(f"{label}：{value}")
        logger.info("输出详情: %s", self._display_name(item))
        return f"{category.upper()} 详细信息\n" + "\n".join(rows)

    async def _handle_search(self, event: AstrMessageEvent, category: str, query: str, items: list[dict[str, Any]]):
        logger.info("收到查询请求，category=%s，query=%s", category, query)
        query = query.strip()
        if not query:
            if category == "gpu":
                logger.info("GPU 空查询，直接发送天梯图")
                async for result in self._send_rank_image(event):
                    yield result
            else:
                logger.info("CPU 空查询，提示输入型号")
                yield event.plain_result(f"请输入要查询的{category.upper()} 型号。")
            return

        matches = self._fuzzy_search_items(items, query)
        if not matches:
            logger.info("未找到匹配结果，query=%s", query)
            yield event.plain_result(f"未找到和 {query} 相关的{category.upper()} 型号。")
            return

        if len(matches) == 1:
            logger.info("唯一命中，直接返回详情")
            yield event.plain_result(self._format_item_detail(category, matches[0]))
            return

        user_id = str(event.get_sender_id())
        self.pending_choices[user_id] = {"category": category, "items": matches}
        lines = [f"找到多个{category.upper()} 候选："]
        for idx, item in enumerate(matches, start=1):
            lines.append(f"{idx}. {self._display_name(item)}")
        logger.info("多候选返回，user_id=%s，数量=%s", user_id, len(matches))
        yield event.plain_result("\n".join(lines))

        @session_waiter(timeout=60, record_history_chains=False)
        async def choose_waiter(controller: SessionController, next_event: AstrMessageEvent):
            if str(next_event.get_sender_id()) != user_id:
                return
            text = next_event.message_str.strip()
            logger.info("收到候选选择输入，user_id=%s，text=%s", user_id, text)
            if text.lower() in {"取消", "退出"}:
                await next_event.send(next_event.plain_result("已取消选择。"))
                self.pending_choices.pop(user_id, None)
                logger.info("用户取消选择，user_id=%s", user_id)
                controller.stop()
                return
            if not text.isdigit():
                await next_event.send(next_event.plain_result("请输入数字序号。"))
                logger.info("用户输入非数字，user_id=%s", user_id)
                return
            index = int(text) - 1
            candidates = self.pending_choices.get(user_id, {}).get("items", [])
            if index < 0 or index >= len(candidates):
                await next_event.send(next_event.plain_result("序号超出范围，请重新输入。"))
                logger.info("用户输入序号越界，user_id=%s，index=%s", user_id, index)
                return
            item = candidates[index]
            await next_event.send(next_event.plain_result(self._format_item_detail(category, item)))
            self.pending_choices.pop(user_id, None)
            logger.info("用户完成选择，user_id=%s，item=%s", user_id, self._display_name(item))
            controller.stop()

        event.stop_event()
        try:
            await choose_waiter(event)
        except TimeoutError:
            self.pending_choices.pop(user_id, None)
            logger.info("候选选择超时，user_id=%s", user_id)
            yield event.plain_result("选择已超时，请重新查询。")

    def _extract_type_and_query(self, text: str) -> tuple[str, str]:
        lowered = text.lower().strip()
        source_type = "laptop" if any(token in lowered for token in ["笔电", "笔记本", "laptop", "mobile"]) else "desktop"
        cleaned = lowered
        for token in ["显卡", "gpu", "相当于", "比较", "对比", "什么", "型号", "对应", "接近", "大概", "约等于", "性能", "台式", "桌面"]:
            cleaned = cleaned.replace(token, " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        logger.info("解析显卡对比请求，source_type=%s，query=%s", source_type, cleaned)
        return source_type, cleaned

    def _gpu_series_rank(self, item: dict[str, Any]) -> int | None:
        match = re.search(r"(\d{4})", str(item.get("name", "")))
        if not match:
            return None
        return int(match.group(1)) // 1000

    def _pick_generation_equivalent(self, base_item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            logger.info("未找到可比较目标候选")
            return None
        base_score = base_item["score"]
        base_series = self._gpu_series_rank(base_item)
        logger.info("开始寻找对位显卡，base=%s，score=%s，series=%s", self._display_name(base_item), base_score, base_series)
        if base_series is None:
            picked = min(candidates, key=lambda item: abs(item["score"] - base_score))
            logger.info("基准显卡无系列信息，按最小分差选择=%s", self._display_name(picked))
            return picked

        for series in range(base_series, 1, -1):
            series_candidates = [item for item in candidates if self._gpu_series_rank(item) == series]
            if not series_candidates:
                logger.info("系列 %s 无候选，继续降级", series)
                continue
            best = min(series_candidates, key=lambda item: abs(item["score"] - base_score))
            diff_ratio = abs(best["score"] - base_score) / max(base_score, 1)
            logger.info("系列 %s 最佳候选=%s，diff_ratio=%.4f", series, self._display_name(best), diff_ratio)
            if diff_ratio <= 0.10:
                return best
        picked = min(candidates, key=lambda item: abs(item["score"] - base_score))
        logger.info("全部系列超过 10%%，回退最小分差选择=%s", self._display_name(picked))
        return picked

    def _format_compare_result(self, base_item: dict[str, Any], target_item: dict[str, Any]) -> str:
        diff_ratio = (target_item["score"] - base_item["score"]) / max(base_item["score"], 1)
        diff_percent = f"{diff_ratio * 100:+.1f}%"
        logger.info("输出显卡对比结果，base=%s，target=%s，diff=%s", self._display_name(base_item), self._display_name(target_item), diff_percent)
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
        """查询 CPU 型号详细信息，模糊匹配并列出候选。"""
        async for result in self._handle_search(event, "cpu", query, self.cpu_items):
            yield result

    @filter.command("gpu")
    async def gpu_search(self, event: AstrMessageEvent, query: str = ""):
        """查询 GPU 型号详细信息，模糊匹配并列出候选。"""
        async for result in self._handle_search(event, "gpu", query, self.gpu_items):
            yield result

    @filter.regex(r"^(?!\s*gpu\b)(?!\s*显卡天梯图\s*$).*(相当于.*显卡|显卡.*相当于|笔电.*台式|台式.*笔电).*$")
    async def compare_gpu(self, event: AstrMessageEvent):
        """比较笔电/台式显卡性能接近型号。"""
        text = event.message_str
        source_type, query = self._extract_type_and_query(text)
        target_type = "desktop" if source_type == "laptop" else "laptop"
        source_candidates = [item for item in self.gpu_items if item.get("type") == source_type]
        target_candidates = [item for item in self.gpu_items if item.get("type") == target_type and item.get("score")]
        base_matches = self._fuzzy_search_items(source_candidates, query)
        if not base_matches:
            logger.info("显卡对比未找到基准型号，query=%s", query)
            yield event.plain_result("未找到比较型号。")
            return
        base_item = base_matches[0]
        target_item = self._pick_generation_equivalent(base_item, target_candidates)
        if not target_item:
            yield event.plain_result("未找到可比较的目标显卡。")
            return
        yield event.plain_result(self._format_compare_result(base_item, target_item))

    @filter.command("显卡天梯图")
    async def gpu_rank(self, event: AstrMessageEvent):
        """发送显卡天梯图。"""
        async for result in self._send_rank_image(event):
            yield result
