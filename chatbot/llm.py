from __future__ import annotations

import json
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from core.config import Settings
from chatbot.prompts import (
    CONVERSATION_ANALYSIS_PROMPT,
    INTENT_PROMPT,
    KEYWORD_PROMPT,
    PRODUCT_RESPONSE_PROMPT,
)


class LLMAnalyzer:
    def __init__(self, settings: Settings) -> None:
        api_key = settings.gemini_api_key
        if not api_key:
            self.model = None
            return

        self.model = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            api_key=api_key,
            temperature=0,
        )
        self.intent_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        INTENT_PROMPT,
                    ),
                    ("human", "{message}"),
                ]
            )
            | self.model
            | StrOutputParser()
        )
        self.keyword_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        KEYWORD_PROMPT,
                    ),
                    ("human", "{message}"),
                ]
            )
            | self.model
            | StrOutputParser()
        )
        self.conversation_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        CONVERSATION_ANALYSIS_PROMPT,
                    ),
                    (
                        "human",
                        "Recent messages:\n{messages}\n\nCurrent message: {current_message}",
                    ),
                ]
            )
            | self.model
            | StrOutputParser()
        )
        self.product_chain = (
            ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        PRODUCT_RESPONSE_PROMPT,
                    ),
                    (
                        "human",
                        "user_query: {query}\nproducts: {products}",
                    ),
                ]
            )
            | self.model
            | StrOutputParser()
        )

    @property
    def available(self) -> bool:
        return self.model is not None

    def classify_intent(self, message: str) -> str | None:
        if not self.available:
            return None
        result = self.intent_chain.invoke({"message": message})
        data = self._load_json(result)
        intent = data.get("intent") if isinstance(data, dict) else None
        print(f"[LLM] Intent data: {data}")
        return intent

    def extract_keywords(
        self, message: str
    ) -> tuple[list[str] | None, str | None, tuple[float | None, float | None]]:
        if not self.available:
            return None, None, (None, None)
        result = self.keyword_chain.invoke({"message": message})
        data = self._load_json(result) or {}
        print(f"[LLM] Keyword payload: {data}")
        keywords = data.get("keywords")
        summary = data.get("query")
        min_price = data.get("min_price")
        max_price = data.get("max_price")

        cleaned: list[str] | None = None
        if isinstance(keywords, list):
            cleaned = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]

        summary_text = summary if isinstance(summary, str) and summary.strip() else None
        min_price_val = float(min_price) if isinstance(min_price, (int, float)) else None
        max_price_val = float(max_price) if isinstance(max_price, (int, float)) else None

        print(f"[LLM] Parsed keywords: {cleaned}, min_price={min_price_val}, max_price={max_price_val}")
        return cleaned, summary_text, (min_price_val, max_price_val)

    def analyze_conversation(self, recent_messages: list[dict], current_message: str) -> str | None:
        if not self.available or not recent_messages:
            return None

        try:
            messages_text = "\n".join(
                [
                    f"{msg.get('role', 'unknown')}: {msg.get('content', '')}"
                    for msg in recent_messages
                ]
            )
            payload = {
                "messages": messages_text,
                "current_message": current_message,
            }
            result = self.conversation_chain.invoke(payload)
            data = self._load_json(result) or {}
            context = data.get("context")
            if isinstance(context, str) and context.strip():
                print(f"[LLM] Conversation context: {context}")
                return context.strip()
            return None
        except Exception as e:
            print(f"[LLM] Error analyzing conversation: {e}")
            return None

    def compose_product_response(self, *, query: str | None, products: list[dict]) -> str | None:
        if not self.available:
            return None
        payload = {
            "query": query or "",
            "products": json.dumps(products, ensure_ascii=False),
        }
        response = self.product_chain.invoke(payload)
        return response.strip() if isinstance(response, str) else None

    @staticmethod
    def _load_json(payload: str) -> dict | None:
        raw = payload.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            # drop opening fence line (e.g. ```json)
            lines = lines[1:]
            # drop closing fence if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print("[LLM] Failed to parse JSON payload", raw)
            return None

