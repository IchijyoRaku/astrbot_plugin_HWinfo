import json
import re
from typing import Any
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

CPU_SINGLE_PATH = Path("data/cpu/r23single.json")
GPU_PATH = Path("data/gpu/timespy.json")
GPU_RANK_IMAGE = Path("data/gpu/显卡天梯图.png")
PLUGIN_VERSION = "0.1"


@register("astrbot_plugin_HWinfo", "IchijyoRaku", "硬件信息与性能对比插件", PLUGIN_VERSION)
class HWInfoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        self.cpu_items = self._load_items(CPU_SINGLE_PATH)
        self.gpu_items = self._load_items(GPU_PATH)
        self.pending_choices: dict[str, dict[str, Any]] = {}
        self.response_pattern = re.compile(r"\{.*\}|\[.*\]", re.S)

    def _load_items(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            logger.warning("数据文件不存在: %s", path)
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("items", [])

    def _display_name(self, item: dict[str, Any]) -> str:
        parts = [item.get("vendor"), item.get("series"), item.get("name")]
        return " ".join(str(part) for part in parts if part and part != "Unknown")

    def _resolve_image_path(self, path: Path | str) -> str:
        return str((Path(__file__).parent / Path(path)).resolve())

    def _image_chain(self, path: Path | str):
        return [Comp.Image.fromFileSystem(self._resolve_image_path(path))]

    async def _send_rank_image(self, event: AstrMessageEvent):
        yield event.chain_result(self._image_chain(GPU_RANK_IMAGE))

    def _item_brief(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": self._display_name(item),
            "vendor": item.get("vendor"),
            "series": item.get("series"),
            "model": item.get("name"),
            "type": item.get("type"),
            "generation": item.get("generation"),
            "score": item.get("score"),
            "release_date": item.get("release_date"),
        }

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

    async def _get_provider_id(self, event: AstrMessageEvent) -> str:
        provider_id = self.config.get("text_provider_id", "") if self.config else ""
        if provider_id:
            return provider_id
        return await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)

    async def _llm_json(self, event: AstrMessageEvent, prompt: str) -> Any:
        llm_resp = await self.context.llm_generate(
            chat_provider_id=await self._get_provider_id(event),
            prompt=prompt,
        )
        text_resp = llm_resp.completion_text.strip()
        matches = self.response_pattern.findall(text_resp)
        payload = matches[0] if matches else text_resp
        return json.loads(payload)

    async def _llm_pick_candidates(
        self,
        event: AstrMessageEvent,
        category: str,
        query: str,
        items: list[dict[str, Any]],
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        candidate_pool = [self._item_brief(item) for item in items[:3000]]
        prompt = f"""
你是硬件检索助手。用户想查询一个 {category.upper()} 型号。
你的任务不是自由回答，而是从候选数据中挑出最可能相关的候选。

用户输入：{query}
候选数据：{json.dumps(candidate_pool, ensure_ascii=False)}

规则：
1. 优先根据型号数字、后缀、品牌、系列来筛选。
2. 类似 5090 应返回 5090、5090D、5090DD 这类候选。
3. 类似 9700x 应返回 9700X、r7 9700x、Ryzen 7 9700X 对应候选。
4. 最多返回 {limit} 个候选。
5. 只返回 JSON 数组，数组元素是候选 name 字符串，不要返回任何解释。

示例输出：
["NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 5090 D"]
"""
        selected_names = await self._llm_json(event, prompt)
        if not isinstance(selected_names, list):
            return []
        name_set = {str(name).strip() for name in selected_names}
        return [item for item in items if self._display_name(item) in name_set][:limit]

    async def _handle_search(self, event: AstrMessageEvent, category: str, query: str, items: list[dict[str, Any]]):
        query = query.strip()
        if not query:
            if category == "gpu":
                async for result in self._send_rank_image(event):
                    yield result
            else:
                yield event.plain_result(f"请输入要查询的{category.upper()} 型号。")
            return

        try:
            matches = await self._llm_pick_candidates(event, category, query, items)
        except Exception as exc:
            logger.error("LLM 候选筛选失败", exc_info=True)
            yield event.plain_result(f"查询失败：{exc}")
            return

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

    async def _llm_pick_equivalent(self, event: AstrMessageEvent, base_item: dict[str, Any], target_items: list[dict[str, Any]]) -> dict[str, Any] | None:
        prompt = f"""
你是显卡性能对比助手。请从候选中选出与基准显卡性能最接近的一个目标显卡。

基准显卡：{json.dumps(self._item_brief(base_item), ensure_ascii=False)}
候选显卡：{json.dumps([self._item_brief(item) for item in target_items], ensure_ascii=False)}

规则：
1. 优先选择与基准显卡同系的显卡，例如 50 系优先匹配 50 系。
2. 如果同系里最近的显卡跑分差距超过 10%，再降级考虑 40 系、30 系、20 系。
3. 只返回一个 JSON 对象，格式：{{"name": "候选显卡全名"}}
4. 不要返回解释。
"""
        result = await self._llm_json(event, prompt)
        if not isinstance(result, dict):
            return None
        selected_name = str(result.get("name", "")).strip()
        for item in target_items:
            if self._display_name(item) == selected_name:
                return item
        return None

    def _format_compare_result(self, base_item: dict[str, Any], target_item: dict[str, Any]) -> str:
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

    def _extract_type_and_query(self, text: str) -> tuple[str, str]:
        lowered = text.lower().strip()
        source_type = "laptop" if any(token in lowered for token in ["笔电", "笔记本", "laptop", "mobile"]) else "desktop"
        return source_type, text.strip()

    @filter.command("cpu")
    async def cpu_search(self, event: AstrMessageEvent, query: str = ""):
        """查询 CPU 型号详细信息，支持 LLM 识别。"""
        async for result in self._handle_search(event, "cpu", query, self.cpu_items):
            yield result

    @filter.command("gpu")
    async def gpu_search(self, event: AstrMessageEvent, query: str = ""):
        """查询 GPU 型号详细信息，支持 LLM 识别。"""
        async for result in self._handle_search(event, "gpu", query, self.gpu_items):
            yield result

    @filter.regex(r"^(?!\s*gpu\b)(?!\s*显卡天梯图\s*$).*(相当于.*显卡|显卡.*相当于|笔电.*台式|台式.*笔电).*$")
    async def compare_gpu(self, event: AstrMessageEvent):
        """比较笔电/台式显卡性能接近型号。"""
        text = event.message_str
        source_type, query = self._extract_type_and_query(text)
        target_type = "desktop" if source_type == "laptop" else "laptop"

        source_matches = await self._llm_pick_candidates(event, "gpu", query, [item for item in self.gpu_items if item.get("type") == source_type], limit=5)
        if not source_matches:
            yield event.plain_result("未找到比较型号。")
            return
        base_item = source_matches[0]

        target_candidates = [item for item in self.gpu_items if item.get("type") == target_type and item.get("score")]
        target_item = await self._llm_pick_equivalent(event, base_item, target_candidates)
        if not target_item:
            yield event.plain_result("未找到可比较的目标显卡。")
            return

        yield event.plain_result(self._format_compare_result(base_item, target_item))

    @filter.command("显卡天梯图")
    async def gpu_rank(self, event: AstrMessageEvent):
        """发送显卡天梯图。"""
        async for result in self._send_rank_image(event):
            yield result
