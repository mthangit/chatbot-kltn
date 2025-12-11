import re

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from chatbot.llm import LLMAnalyzer
from chatbot.memory import ConversationMemory
from chatbot.rag import QdrantRAG
from chatbot.redis_memory import RedisConversationMemory
from chatbot.state import ChatbotState
from chatbot.tools import get_user_orders, get_user_profile, search_products_by_keyword

INTENT_KEYWORDS = {
    "orders": ["order", "orders", "tracking", "shipment"],
    "profile": ["profile", "account", "information", "user"],
}

STOP_WORDS = {
    "i",
    "me",
    "my",
    "want",
    "to",
    "buy",
    "please",
    "show",
    "find",
    "need",
    "order",
    "product",
}


def _analyze_conversation(
    ai: LLMAnalyzer | None, redis_memory: RedisConversationMemory | None, state: ChatbotState
) -> ChatbotState:
    session_id = state.get("session_id", "")
    current_message = state.get("message", "")
    print(f"[LangGraph] Analyzing conversation for session {session_id}")

    if redis_memory and redis_memory.available:
        recent_messages = redis_memory.get_recent_messages(session_id, limit=5)
        state["recent_messages"] = recent_messages

        if recent_messages and ai and ai.available:
            context = ai.analyze_conversation(recent_messages, current_message)
            state["conversation_context"] = context
            print(f"[LangGraph] Conversation context: {context}")
        else:
            state["conversation_context"] = None
            print("[LangGraph] No recent messages or AI unavailable, skipping conversation analysis")
    else:
        state["recent_messages"] = []
        state["conversation_context"] = None
        print("[LangGraph] Redis memory unavailable, skipping conversation analysis")

    return state


def _detect_intent(ai: LLMAnalyzer | None, state: ChatbotState) -> ChatbotState:
    message = state.get("message", "")
    context = state.get("conversation_context")
    print(f"[LangGraph] Detect intent for message: {message}")
    if context:
        print(f"[LangGraph] Using conversation context: {context}")

    full_message = f"{context}\n\n{message}" if context else message
    intent = ai.classify_intent(full_message) if ai and ai.available else None
    if not intent:
        lowered = message.lower()
        for candidate, keywords in INTENT_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                intent = candidate
                break
        else:
            intent = "product_search"
    state["intent"] = intent
    print(f"[LangGraph] Intent resolved to: {state['intent']}")
    return state


def _extract_keywords(ai: LLMAnalyzer | None, state: ChatbotState) -> ChatbotState:
    message = state.get("message", "")
    if ai and ai.available:
        print("[LangGraph] Using LLM to extract keywords")
        keywords, summary, (min_price, max_price) = ai.extract_keywords(message)
        keywords = keywords or []
    else:
        print("[LangGraph] Falling back to regex keyword extraction")
        words = re.findall(r"[a-zA-Z0-9]+", message.lower())
        keywords = [word for word in words if word not in STOP_WORDS]
        if not keywords and message:
            keywords = words
        summary = message
        min_price = None
        max_price = None
    state["keywords"] = keywords
    state["product_query"] = summary
    state["min_price"] = min_price
    state["max_price"] = max_price
    print(f"[LangGraph] Extracted keywords: {keywords}")
    return state


def run_tools(state: ChatbotState, db: Session, rag: QdrantRAG | None = None) -> ChatbotState:
    intent = state.get("intent")
    print(f"[LangGraph] Running tools for intent: {intent}")
    if intent == "orders" and state.get("user_id"):
        print("[LangGraph] Fetching order history")
        state["tool_result"] = {"orders": get_user_orders(db, state["user_id"])}
    elif intent == "profile" and state.get("user_id"):
        print("[LangGraph] Fetching user profile")
        state["tool_result"] = {"profile": get_user_profile(db, state["user_id"])}
    else:
        print("[LangGraph] Searching products")
        query_text = state.get("product_query") or state.get("message", "")
        state["tool_result"] = {
            "products": search_products_by_keyword(
                db,
                state.get("keywords"),
                min_price=state.get("min_price"),
                max_price=state.get("max_price"),
                rag=rag,
                query_text=query_text,
            )
        }
    print(f"[LangGraph] Tool result keys: {list((state.get('tool_result') or {}).keys())}")
    return state


def craft_response(
    state: ChatbotState,
    memory: ConversationMemory,
    ai: LLMAnalyzer | None,
    redis_memory: RedisConversationMemory | None,
) -> ChatbotState:
    intent = state.get("intent")
    result = state.get("tool_result") or {}
    print(f"[LangGraph] Crafting response for intent: {intent}")

    if intent == "orders":
        orders = result.get("orders", [])
        if orders:
            reply = "Đây là các đơn hàng gần đây của bạn:"
        else:
            reply = "Bạn chưa có đơn hàng nào. Hãy đặt hàng để bắt đầu mua sắm nhé!"
    elif intent == "profile":
        profile = result.get("profile")
        if profile:
            reply = "Thông tin tài khoản của bạn:"
        else:
            reply = "Không tìm thấy thông tin tài khoản. Vui lòng kiểm tra lại."
    elif intent == "product_search" and result.get("products"):
        names = ", ".join(
            p["product_name"] for p in result["products"] if p.get("product_name")
        )
        query_desc = state.get("product_query")
        ai_reply = (
            ai.compose_product_response(query=query_desc, products=result.get("products", []))
            if ai and ai.available
            else None
        )
        if ai_reply:
            reply = ai_reply
        else:
            prefix = f"You asked about {query_desc}. " if query_desc else ""
            reply = (
                f"{prefix}I found these products: {names}"
                if names
                else f"{prefix}No active products found."
            )
    else:
        reply = "I could not find relevant data but I am still ready to help."

    memory.append(state["session_id"], "assistant", reply)

    if redis_memory and redis_memory.available:
        session_id = state.get("session_id", "")
        user_message = state.get("message", "")
        redis_memory.append(session_id, "user", user_message)
        redis_memory.append(session_id, "assistant", reply)
        print(f"[LangGraph] Saved messages to Redis for session {session_id}")

    print(f"[LangGraph] Reply generated: {reply}")
    state["response"] = reply
    return state


def build_graph(
    db: Session,
    memory: ConversationMemory,
    ai: LLMAnalyzer | None,
    rag: QdrantRAG | None = None,
    redis_memory: RedisConversationMemory | None = None,
) -> StateGraph:
    graph = StateGraph(ChatbotState)

    graph.add_node("analyze", lambda state: _analyze_conversation(ai, redis_memory, state))
    graph.add_node("intent", lambda state: _detect_intent(ai, state))
    graph.add_node("keywords", lambda state: _extract_keywords(ai, state))
    graph.add_node("tools", lambda state: run_tools(state, db, rag))
    graph.add_node("response", lambda state: craft_response(state, memory, ai, redis_memory))

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "intent")
    graph.add_edge("intent", "keywords")
    graph.add_edge("keywords", "tools")
    graph.add_edge("tools", "response")
    graph.add_edge("response", END)

    return graph.compile()
