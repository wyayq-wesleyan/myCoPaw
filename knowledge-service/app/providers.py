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

    def extract_facts(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.settings.chat_base_url or not self.settings.chat_model:
            return self._heuristic_extract(chunks)
        source = "\n\n".join(f"[片段 {index}] {chunk['content']}" for index, chunk in enumerate(chunks))
        prompt = (
            "你是汇智小助手的知识抽取器。仅根据给定材料抽取实体和明确关系。"
            "人物即使只出现在表格中也必须抽取。不要把推测写成事实。"
            "只返回 JSON 对象：{\"entities\":[{\"name\":...,\"type\":\"person|event|organization|place|case|other\","
            "\"role\":...,\"chunk_index\":0,\"confidence\":0.0}],\"relations\":[{\"subject\":...,\"predicate\":...,\"object\":...,\"confidence\":0.0}]}。\n\n"
            + source[:50000]
        )
        payload = {"model": self.settings.chat_model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        try:
            response = httpx.post(f"{self.settings.chat_base_url}/chat/completions", headers=self._headers(self.settings.chat_api_key), json=payload, timeout=180)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except Exception:
            return self._heuristic_extract(chunks)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.settings.embedding_base_url or not self.settings.embedding_model:
            return None
        payload: dict[str, Any] = {"model": self.settings.embedding_model, "input": texts}
        if self.settings.embedding_dimensions:
            payload["dimensions"] = self.settings.embedding_dimensions
        try:
            response = httpx.post(f"{self.settings.embedding_base_url}/embeddings", headers=self._headers(self.settings.embedding_api_key), json=payload, timeout=120)
            response.raise_for_status()
            data = response.json().get("data", [])
            return [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        except Exception:
            return None

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
        prompt = (
            "你是汇智小助手。请仅根据提供的证据回答问题。区分明确事实、可能关联和冲突信息，"
            "不要补造材料中没有的内容。回答中引用文件名和页码。\n"
            f"问题：{query}\n\n关系记录：\n{relation_text}\n\n证据：\n{evidence_text}"
        )
        payload = {"model": self.settings.chat_model, "temperature": 0.1, "messages": [{"role": "user", "content": prompt}]}
        try:
            response = httpx.post(f"{self.settings.chat_base_url}/chat/completions", headers=self._headers(self.settings.chat_api_key), json=payload, timeout=300)
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
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
        prompt = (
            "你是汇智小助手。请仅根据提供的证据回答问题。区分明确事实、可能关联和冲突信息，"
            "不要补造材料中没有的内容。回答中引用文件名和页码。\n"
            f"问题：{query}\n\n关系记录：\n{relation_text}\n\n证据：\n{evidence_text}"
        )
        payload = {
            "model": self.settings.chat_model,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
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
