import json
import re
from difflib import SequenceMatcher
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
FONT_FAMILY = "SourceHanSansCN-Regular"

DETAIL_TEMPLATE = """
<div style="background:#ffffff;color:#111111;padding:36px 42px;font-family:'{{ font_family }}','Microsoft YaHei',sans-serif;width:900px;box-sizing:border-box;">
  <div style="font-size:38px;font-weight:700;margin-bottom:10px;">{{ title }}</div>
  <div style="font-size:22px;color:#666;margin-bottom:24px;">{{ subtitle }}</div>
  <table style="width:100%;border-collapse:collapse;font-size:24px;">
    {% for row in rows %}
    <tr>
      <td style="padding:12px 10px;border:1px solid #ddd;background:#f7f7f7;width:240px;font-weight:700;">{{ row.label }}</td>
      <td style="padding:12px 14px;border:1px solid #ddd;">{{ row.value }}</td>
    </tr>
    {% endfor %}
  </table>
  <div style="font-size:18px;color:rgba(0,0,0,0.55);margin-top:18px;">tip：数据来源 topcpu，仅供参考</div>
</div>
"""

COMPARE_TEMPLATE = """
<div style="background:#ffffff;color:#111111;padding:36px 42px;font-family:'{{ font_family }}','Microsoft YaHei',sans-serif;width:980px;box-sizing:border-box;">
  <div style="font-size:38px;font-weight:700;margin-bottom:12px;">{{ title }}</div>
  <div style="font-size:28px;font-weight:600;margin-bottom:16px;">{{ conclusion }}</div>
  <div style="font-size:22px;color:#555;margin-bottom:18px;">基准显卡：{{ base_name }} ｜ 跑分：{{ base_score }}</div>
  <table style="width:100%;border-collapse:collapse;font-size:24px;">
    <tr>
      <th style="padding:12px;border:1px solid #ddd;background:#f0f0f0;">型号</th>
      <th style="padding:12px;border:1px solid #ddd;background:#f0f0f0;">类型</th>
      <th style="padding:12px;border:1px solid #ddd;background:#f0f0f0;">代际</th>
      <th style="padding:12px;border:1px solid #ddd;background:#f0f0f0;">Score</th>
      <th style="padding:12px;border:1px solid #ddd;background:#f0f0f0;">相对基准</th>
    </tr>
    {% for row in rows %}
    <tr>
      <td style="padding:12px;border:1px solid #ddd;">{{ row.name }}</td>
      <td style="padding:12px;border:1px solid #ddd;text-transform:capitalize;">{{ row.type }}</td>
      <td style="padding:12px;border:1px solid #ddd;">{{ row.generation or '-' }}</td>
      <td style="padding:12px;border:1px solid #ddd;">{{ row.score }}</td>
      <td style="padding:12px;border:1px solid #ddd;">{{ row.percent }}</td>
    </tr>
    {% endfor %}
  </table>
  <div style="font-size:18px;color:rgba(0,0,0,0.55);margin-top:18px;">tip：数据来源 topcpu，仅供参考</div>
</div>
"""


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

    def _normalize_query(self, text: str) -> str:
        text = text.lower().strip()
        replacements = {
            " ": "",
            "-": "",
            "_": "",
            "geforce": "",
            "radeon": "",
            "rtx": "",
            "gtx": "",
            "rx": "",
            "super": "s",
            "ti": "ti",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)
        return text

    def _item_search_text(self, item: dict[str, Any]) -> str:
        parts = [
            str(item.get("vendor", "")),
            str(item.get("series", "")),
            str(item.get("name", "")),
            str(item.get("generation", "")),
            str(item.get("type", "")),
        ]
        return self._normalize_query(" ".join(parts))

    def _score_candidate(self, query: str, item: dict[str, Any]) -> float:
        normalized_query = self._normalize_query(query)
        target = self._item_search_text(item)
        if not normalized_query or not target:
            return 0.0
        if normalized_query in target:
            return 10 + len(normalized_query) / max(len(target), 1)
        return SequenceMatcher(None, normalized_query, target).ratio()

    def _search_items(self, items: list[dict[str, Any]], query: str, limit: int = 8) -> list[dict[str, Any]]:
        scored = []
        for item in items:
            score = self._score_candidate(query, item)
            if score >= 0.45:
                scored.append((score, item))
        scored.sort(key=lambda x: (-x[0], x[1].get("rank", 999999)))
        return [item for _, item in scored[:limit]]

    def _display_name(self, item: dict[str, Any]) -> str:
        parts = [item.get("vendor"), item.get("series"), item.get("name")]
        return " ".join(str(p) for p in parts if p and p != "Unknown")

    async def _render_detail_image(self, category: str, item: dict[str, Any]) -> str:
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
            rows.append({"label": label, "value": value})

        return await self.html_render(
            DETAIL_TEMPLATE,
            {
                "font_family": FONT_FAMILY,
                "title": self._display_name(item),
                "subtitle": f"{category.upper()} 详细信息",
                "rows": rows,
            },
            options={},
        )

    async def _handle_search(self, event: AstrMessageEvent, category: str, query: str, items: list[dict[str, Any]]):
        matches = self._search_items(items, query)
        if not matches:
            yield event.plain_result(f"未找到和 {query} 相关的{category.upper()} 型号。")
            return

        if len(matches) == 1:
            image_url = await self._render_detail_image(category, matches[0])
            yield event.image_result(image_url)
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
            image_url = await self._render_detail_image(category, item)
            await next_event.send(next_event.image_result(image_url))
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
        text = text.strip()
        gpu_type = "laptop" if any(word in text for word in ["笔电", "笔记本", "laptop", "mobile"] ) else "desktop"
        cleaned = text
        for token in ["笔电", "笔记本", "laptop", "mobile", "台式", "desktop", "显卡", "gpu", "相当于", "什么", "型号", "的"]:
            cleaned = cleaned.replace(token, " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return gpu_type, cleaned

    def _find_equivalent_gpus(self, base_item: dict[str, Any], target_type: str) -> list[dict[str, Any]]:
        base_score = base_item["score"]
        candidates = [
            item for item in self.gpu_items
            if item.get("type") == target_type and item.get("generation")
        ]
        within = []
        for item in candidates:
            diff_ratio = abs(item["score"] - base_score) / base_score
            if diff_ratio <= 0.05:
                within.append((diff_ratio, item))
        if within:
            within.sort(key=lambda x: (x[0], abs(x[1]["score"] - base_score)))
            return [within[0][1]]

        lower = None
        higher = None
        for item in candidates:
            if item["score"] < base_score:
                if lower is None or item["score"] > lower["score"]:
                    lower = item
            elif item["score"] > base_score:
                if higher is None or item["score"] < higher["score"]:
                    higher = item
        result = []
        if lower:
            result.append(lower)
        if higher:
            result.append(higher)
        return result

    async def _render_compare_image(self, base_item: dict[str, Any], target_items: list[dict[str, Any]]) -> str:
        base_name = self._display_name(base_item)
        rows = []
        all_items = [base_item] + target_items
        for item in all_items:
            percent = f"{round(item['score'] / base_item['score'] * 100)}%"
            rows.append(
                {
                    "name": self._display_name(item),
                    "type": item.get("type", "-"),
                    "generation": item.get("generation"),
                    "score": item.get("score"),
                    "percent": percent,
                }
            )
        conclusion = f"结论：{base_name} 相当于 " + " / ".join(self._display_name(item) for item in target_items)
        return await self.html_render(
            COMPARE_TEMPLATE,
            {
                "font_family": FONT_FAMILY,
                "title": "显卡性能对比",
                "conclusion": conclusion,
                "base_name": base_name,
                "base_score": base_item["score"],
                "rows": rows,
            },
            options={},
        )

    @filter.command("cpu")
    async def cpu_search(self, event: AstrMessageEvent, query: str):
        """查询 CPU 型号详细信息，支持模糊搜索。"""
        async for result in self._handle_search(event, "cpu", query, self.cpu_items):
            yield result

    @filter.command("gpu")
    async def gpu_search(self, event: AstrMessageEvent, query: str):
        """查询 GPU 型号详细信息，支持模糊搜索。"""
        async for result in self._handle_search(event, "gpu", query, self.gpu_items):
            yield result

    @filter.regex(r".*相当于.*显卡.*|.*显卡.*相当于.*|.*笔电.*台式.*|.*台式.*笔电.*")
    async def compare_gpu(self, event: AstrMessageEvent):
        """比较笔电/台式显卡性能接近型号。"""
        text = event.message_str
        source_type, query = self._extract_type_and_query(text)
        target_type = "desktop" if source_type == "laptop" else "laptop"
        matches = [item for item in self._search_items(self.gpu_items, query, limit=20) if item.get("type") == source_type]
        if not matches:
            yield event.plain_result("未找到用于比较的显卡型号。")
            return
        base_item = matches[0]
        target_items = self._find_equivalent_gpus(base_item, target_type)
        if not target_items:
            yield event.plain_result("未找到可比较的目标显卡。")
            return
        image_url = await self._render_compare_image(base_item, target_items)
        yield event.image_result(image_url)

    @filter.command("显卡天梯图")
    async def gpu_rank(self, event: AstrMessageEvent):
        """发送显卡天梯图。"""
        chain = [Comp.Image.fromFileSystem(str(GPU_RANK_IMAGE))]
        yield event.chain_result(chain)
