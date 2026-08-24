from __future__ import annotations

import json
import re
from typing import Any, Generator

import httpx

from .config import Settings


class ModelClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # 每批送入 LLM 的字符上限。超过会截断导致大文档实体覆盖不全，
    # 分批处理保证全部内容参与抽取。
    EXTRACT_BATCH_CHARS = 12000
    # 并行批次上限：模型服务（MLX 单卡）并发过高会互相排队，2-3 为实测较优值
    EXTRACT_MAX_WORKERS = 3

    def _extract_batch(self, batch: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        source = "\n\n".join(f"[片段 {index}] {chunk['content']}" for index, chunk in batch)
        prompt = (
            "你是汇智小助手的知识抽取器。仅根据给定材料抽取实体和明确关系。"
            "人物即使只出现在表格中也必须抽取。不要把推测写成事实。"
            "只返回 JSON 对象：{\"entities\":[{\"name\":...,\"type\":\"person|event|organization|place|case|other\","
            "\"role\":...,\"chunk_index\":0,\"confidence\":0.0}],\"relations\":[{\"subject\":...,\"predicate\":...,\"object\":...,\"confidence\":0.0}]}。\n\n"
            + source
        )
        payload = {"model": self.settings.chat_model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        try:
            response = httpx.post(
                f"{self.settings.chat_base_url}/chat/completions",
                headers=self._headers(self.settings.chat_api_key),
                json=payload,
                timeout=300,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except Exception:
            return {}

    def extract_facts(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.settings.chat_base_url or not self.settings.chat_model:
            return self._heuristic_extract(chunks)
        batches: list[list[tuple[int, dict[str, Any]]]] = []
        current: list[tuple[int, dict[str, Any]]] = []
        current_chars = 0
        for index, chunk in enumerate(chunks):
            piece = f"[片段 {index}] {chunk['content']}"
            if current and current_chars + len(piece) > self.EXTRACT_BATCH_CHARS:
                batches.append(current)
                current = []
                current_chars = 0
            current.append((index, chunk))
            current_chars += len(piece)
        if current:
            batches.append(current)

        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        if len(batches) > 1 and self.EXTRACT_MAX_WORKERS > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=self.EXTRACT_MAX_WORKERS) as pool:
                results = list(pool.map(self._extract_batch, batches))
        else:
            results = [self._extract_batch(batch) for batch in batches]
        for parsed in results:
            entities.extend(parsed.get("entities", []))
            relations.extend(parsed.get("relations", []))

        if not entities:
            return self._heuristic_extract(chunks)
        return {"entities": entities, "relations": relations}

    # 单次 embedding 请求的文本条数上限，避免大文档一次性请求超时
    EMBED_BATCH_SIZE = 32

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.settings.embedding_base_url or not self.settings.embedding_model:
            return None
        all_vectors: list[list[float]] | None = None
        for start in range(0, len(texts), self.EMBED_BATCH_SIZE):
            batch = texts[start : start + self.EMBED_BATCH_SIZE]
            payload: dict[str, Any] = {"model": self.settings.embedding_model, "input": batch}
            if self.settings.embedding_dimensions:
                payload["dimensions"] = self.settings.embedding_dimensions
            try:
                response = httpx.post(f"{self.settings.embedding_base_url}/embeddings", headers=self._headers(self.settings.embedding_api_key), json=payload, timeout=120)
                response.raise_for_status()
                data = response.json().get("data", [])
                batch_vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
            except Exception:
                return None
            if all_vectors is None:
                all_vectors = []
            all_vectors.extend(batch_vectors)
            if len(batch_vectors) != len(batch):
                break
        return all_vectors

    def describe_image_chunks(self, chunks: list[dict[str, Any]]) -> int:
        """Describe extracted document images when the configured model accepts vision input.

        MinerU provides image bytes and captions for image blocks. Captions are
        retained as a fallback, while a vision-capable OpenAI-compatible model
        can add the actual chart/screenshot content to the text index.
        """
        if not self.settings.chat_base_url or not self.settings.chat_model:
            return 0
        described = 0
        for chunk in chunks:
            image_data = chunk.get("_image_data")
            if not isinstance(image_data, str) or not image_data.startswith("data:image/"):
                continue
            prompt = (
                "请描述这张业务文档图片，提取可见的标题、文字、表格、流程、数值、坐标轴和关键关系。"
                "只写客观可检索的事实；看不清的内容明确说明，不要猜测。"
            )
            payload = {
                "model": self.settings.chat_model,
                "temperature": 0,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ],
                }],
            }
            try:
                response = httpx.post(
                    f"{self.settings.chat_base_url}/chat/completions",
                    headers=self._headers(self.settings.chat_api_key),
                    json=payload,
                    timeout=180,
                )
                response.raise_for_status()
                value = response.json()["choices"][0]["message"]["content"]
                if isinstance(value, list):
                    value = " ".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
                description = str(value).strip()
                if description:
                    original = str(chunk.get("content") or "").strip()
                    chunk["content"] = f"{original}\n[图片视觉描述] {description}" if original else f"[图片视觉描述] {description}"
                    described += 1
            except Exception:
                # A text-only model must not make document ingestion fail.
                continue
        return described

    @staticmethod
    def _strip_meta(text: str) -> str:
        """移除模型常见的英文元描述后缀"""
        markers = ["This task is", "Summary of Findings", "Final Status:", "No further tool"]
        for marker in markers:
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx]
        return text.rstrip()

    def answer(self, query: str, evidence: list[dict[str, Any]], relations: list[dict[str, Any]]) -> str:
        if not self.settings.chat_base_url or not self.settings.chat_model:
            return "未配置对话模型。已找到以下相关材料：\n" + "\n".join(
                f"- {item.get('filename', '')}：{item.get('content', '')[:240]}" for item in evidence[:10]
            )
        evidence_text = "\n\n".join(
            f"来源：{item.get('filename')}，第 {item.get('page') or '?'} 页，{item.get('location') or ''}\n{item.get('content', '')}"
            for item in evidence[:30]
        )
        relation_text = "\n".join(f"{item['subject']} --{item['predicate']}--> {item['object']}" for item in relations[:50])
        system_msg = (
            "你是汇智小助手，一个内网知识库问答助手。"
            "必须用提问的语言回答（中文提问用中文回答，英文提问用英文回答）。"
            "只输出一种语言，绝不要同时给出中英文两份答案。"
            "绝不要输出英文总结、元描述或任务完成声明。直接回答问题。"
        )
        user_msg = (
            "请仅根据提供的证据回答问题。区分明确事实、可能关联和冲突信息，"
            "不要补造材料中没有的内容。回答中引用文件名和页码。\n"
            f"问题：{query}\n\n关系记录：\n{relation_text}\n\n证据：\n{evidence_text}"
        )
        payload = {
            "model": self.settings.chat_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "stop": ["This task is", "Summary of Findings", "Final Status:"],
        }
        try:
            response = httpx.post(f"{self.settings.chat_base_url}/chat/completions", headers=self._headers(self.settings.chat_api_key), json=payload, timeout=300)
            response.raise_for_status()
            content = str(response.json()["choices"][0]["message"]["content"])
            return self._strip_meta(content)
        except Exception as exc:
            return f"模型回答暂不可用，已返回检索证据。原因：{exc}"

    def answer_stream(self, query: str, evidence: list[dict[str, Any]], relations: list[dict[str, Any]]) -> Generator[str, None, None]:
        """流式生成回答"""
        if not self.settings.chat_base_url or not self.settings.chat_model:
            yield "未配置对话模型。已找到以下相关材料：\n" + "\n".join(
                f"- {item.get('filename', '')}：{item.get('content', '')[:240]}" for item in evidence[:10]
            )
            return

        evidence_text = "\n\n".join(
            f"来源：{item.get('filename')}，第 {item.get('page') or '?'} 页，{item.get('location') or ''}\n{item.get('content', '')}"
            for item in evidence[:30]
        )
        relation_text = "\n".join(f"{item['subject']} --{item['predicate']}--> {item['object']}" for item in relations[:50])
        system_msg = (
            "你是汇智小助手，一个内网知识库问答助手。"
            "必须用提问的语言回答（中文提问用中文回答，英文提问用英文回答）。"
            "只输出一种语言，绝不要同时给出中英文两份答案。"
            "绝不要输出英文总结、元描述或任务完成声明。直接回答问题。"
        )
        user_msg = (
            "请仅根据提供的证据回答问题。区分明确事实、可能关联和冲突信息，"
            "不要补造材料中没有的内容。回答中引用文件名和页码。\n"
            f"问题：{query}\n\n关系记录：\n{relation_text}\n\n证据：\n{evidence_text}"
        )
        payload = {
            "model": self.settings.chat_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "stream": True,
            "stop": ["This task is", "Summary of Findings", "Final Status:"],
        }
        try:
            with httpx.Client(timeout=300) as client:
                with client.stream(
                    "POST",
                    f"{self.settings.chat_base_url}/chat/completions",
                    headers=self._headers(self.settings.chat_api_key),
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
        except Exception as exc:
            yield f"模型回答暂不可用，已返回检索证据。原因：{exc}"

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
        text = str(content).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"entities": [], "relations": []}
        try:
            parsed = json.loads(match.group(0))
            return {"entities": parsed.get("entities", []), "relations": parsed.get("relations", [])}
        except json.JSONDecodeError:
            return {"entities": [], "relations": []}

    @staticmethod
    def _heuristic_extract(chunks: list[dict[str, Any]]) -> dict[str, Any]:
        entities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, chunk in enumerate(chunks):
            for name in re.findall(r"[\u4e00-\u9fff]{1,3}某", chunk["content"]):
                if name not in seen:
                    seen.add(name)
                    entities.append({"name": name, "type": "person", "role": "材料提及", "chunk_index": index, "confidence": 0.4})
        return {"entities": entities, "relations": []}
