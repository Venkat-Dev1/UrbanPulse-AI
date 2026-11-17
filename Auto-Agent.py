from dotenv import load_dotenv
from langchain.messages import ToolMessage
load_dotenv()
import os
import requests
from langchain_groq import ChatGroq
from langchain_core.tools import tool
from tavily import TavilyClient
from langchain.agents import create_agent
from rich import print
from langchain.agents.middleware import wrap_tool_call

# Now lets create some tools

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

@wrap_tool_call
def human_approval(request,handler):
    """Ask for human approval before call any tool"""
    tool_name=request.tool_call["name"]
    confirm=input(f"Agent wants to call {tool_name} . Approve yes / no :-")
    if confirm.lower() !='yes':
        return ToolMessage(
            content=f" The user doesn't approve the {tool_name} tool",
            tool_call_id=request.tool_call["id"]
        )
    return handler(request)

# LLM
llm = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    max_tokens=500,
)

# Create the agent using create_react_agent (auto tool calling, no manual loop needed)
agent = create_agent(
    model=llm,
    tools=[get_weather, get_news],
    system_prompt="You are a helpful AI assistant. Use the available tools to answer questions about weather and news for cities.",
    middleware=[human_approval]
)

# Chat loop
print("Welcome to city agent \n Type exit to quit")

while True:
    user_input = input("\nYou: ")
    if user_input.lower() == "exit":
        print("Thank you for using. GOODBYE 🥹")
        break

    result = agent.invoke(
        {"messages": [{"role": "user", "content": user_input}]}
    )

    # The last message in the result is the AI's final response
    final_message = result["messages"][-1]
    print(f"\nAI: {final_message.content}")
