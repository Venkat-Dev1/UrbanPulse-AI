import os
import asyncio
import queue
import contextvars

import requests
from dotenv import load_dotenv

load_dotenv()

from langchain.messages import ToolMessage
from langchain_groq import ChatGroq
from langchain_core.tools import tool
from tavily import TavilyClient
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Weather Tool
@tool
def get_weather(city: str) -> str:
    """Get the current weather for a given city."""
    API_KEY = os.getenv("OPENWEATHER_API_KEY")
    url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_KEY}&units=metric"
    response = requests.get(url)
    data = response.json()
    if data["cod"] != 200:
        return f"Error: {data['message']}"
    weather_description = data["weather"][0]["description"]
    temperature = data["main"]["temp"]
    return f"The current weather in {city} is {weather_description} with a temperature of {temperature}°C."


# Tavily search tool
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


@tool
def get_news(city: str) -> str:
    """Get the latest news for a given city."""

    try:
        response = tavily_client.search(
            query=f"Latest news in {city}",
            topic="news",
            time_range="day",
            max_results=5
        )

        news = response.get("results", [])

        if not news:
            return f"No news found for {city}."

        points = [f"{i}. {item['title']}" for i, item in enumerate(news[:5], 1)]
        source_url = news[0].get("url", "")
        return "\n".join(points) + f"\n\nSource: {source_url}"

    except Exception as e:
        return f"Error fetching news: {str(e)}"




# Holds the WebSocket session of the request currently being processed.
# The agent runs in a worker thread, and the middleware reads it from here.
_current_session: contextvars.ContextVar = contextvars.ContextVar("current_session")


class Session:
    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.loop = loop
        self.approvals: "queue.Queue[bool]" = queue.Queue()

    def ask_approval(self, tool_name: str, args: dict) -> bool:
        """Called from the agent's worker thread. Blocks until the browser answers."""
        asyncio.run_coroutine_threadsafe(
            self.ws.send_json(
                {"type": "approval_request", "tool": tool_name, "args": args}
            ),
            self.loop,
        ).result()
        return self.approvals.get()


# Same middleware as before - only the source of the yes/no changed
# (browser button instead of console input()).
@wrap_tool_call
def human_approval(request, handler):
    """Ask for human approval before call any tool"""
    tool_name = request.tool_call["name"]
    session: Session = _current_session.get()
    approved = session.ask_approval(tool_name, request.tool_call.get("args", {}))
    if not approved:
        return ToolMessage(
            content=f" The user doesn't approve the {tool_name} tool",
            tool_call_id=request.tool_call["id"]
        )
    return handler(request)


# ----------------------------------------------------------------------
# LLM + Agent
# ----------------------------------------------------------------------
llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    max_tokens=500,
)

agent = create_agent(
    model=llm,
    tools=[get_weather, get_news],
    system_prompt="You are a helpful AI assistant. Use the available tools to answer questions about weather and news for cities.",
    middleware=[human_approval]
)


# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------
app = FastAPI(title="City Agent")


@app.get("/")
async def index():
    html_path = os.path.join(BASE_DIR, "static", "index.html")
    if not os.path.exists(html_path):
        html_path = os.path.join(BASE_DIR, "index.html")
    return FileResponse(html_path)


static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


async def run_agent(session: Session, user_input: str):
    _current_session.set(session)  # task has its own context copy; to_thread inherits it
    try:
        result = await asyncio.to_thread(
            agent.invoke,
            {"messages": [{"role": "user", "content": user_input}]},
        )
        # The last message in the result is the AI's final response
        final_message = result["messages"][-1]
        await session.ws.send_json({"type": "answer", "content": final_message.content})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await session.ws.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(ws, asyncio.get_running_loop())
    task: asyncio.Task | None = None

    try:
        while True:
            msg = await ws.receive_json()

            if msg.get("type") == "chat":
                if task and not task.done():
                    continue  # one request at a time per connection
                text = (msg.get("content") or "").strip()
                if text:
                    task = asyncio.create_task(run_agent(session, text))

            elif msg.get("type") == "approval":
                session.approvals.put(bool(msg.get("approved")))

    except WebSocketDisconnect:
        pass
    finally:
        # Unblock a worker thread that may be waiting on an approval
        session.approvals.put(False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
