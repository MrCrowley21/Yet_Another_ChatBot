import os
import uuid
import asyncio
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, END, StateGraph
from langchain.schema import SystemMessage
from langchain_google_community import GoogleSearchAPIWrapper
from langgraph.prebuilt import ToolNode
from langchain_core.tools import Tool
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import AIMessageChunk, HumanMessage, RemoveMessage, ToolMessage
from typing import Annotated
import nest_asyncio
from dotenv import dotenv_values

from prompt import INSTRUCTION_PROMPT, SUMMARIZER_PROMPT

# Prevent nested event loops
nest_asyncio.apply()

# Initialize environmental variables

### For Docker ###
# configs = dotenv_values()
# openai_key = configs["OPENAI_API_KEY"]
# google_cse_id = configs["GOOGLE_CSE_ID"]
# google_api_key = configs["GOOGLE_API_KEY"]
# model_name = configs["MODEL_NAME"]
# model_temperature = configs["TEMPERATURE"]
# langchain_tracing = configs["LANGCHAIN_TRACING_V2"]
# langchain_api_key = configs["LANGCHAIN_API_KEY"]
# langchain_project_name = configs["LANGCHAIN_PROJECT"]

### For Deployment ###
openai_key = os.getenv("OPENAI_API_KEY")
google_cse_id = os.getenv("GOOGLE_CSE_ID")
google_api_key = os.getenv("GOOGLE_API_KEY")
model_name = os.getenv("MODEL_NAME")
model_temperature = os.getenv("TEMPERATURE")
langchain_tracing = os.getenv("LANGCHAIN_TRACING_V2")
langchain_api_key = os.getenv("LANGCHAIN_API_KEY")
langchain_project_name = os.getenv("LANGCHAIN_PROJECT")

# Connect to the LangSmith
os.environ["LANGCHAIN_TRACING_V2"] = langchain_tracing
os.environ["LANGCHAIN_API_KEY"] = langchain_api_key
os.environ["LANGCHAIN_PROJECT"] = langchain_project_name


class State(TypedDict):
    """The constructor for the graph state"""
    summary: str
    messages: Annotated[list, add_messages]


async def call_model(state: State, config: RunnableConfig, lock: asyncio.Lock):
    """Handles user queries by invoking LLM and searching through web given provided engine
       Args:
           state (State): the state of the constructed graph
           config (RunnableConfig): the configuration of the current thread
           lock (asyncio.Lock): the lock object for the agent direct interaction
       Returns:
           dict: the updates in the current state of the graph
    """
    system_prompt = INSTRUCTION_PROMPT
    tool_node, model_with_tools, model = initialize_model(model_name)
    messages = [SystemMessage(content=system_prompt)] + state["messages"]
    messages.extend(state["messages"])
    async with lock:
        response = await model_with_tools.ainvoke(messages, config)
    return {"messages": response}


async def summarize_chat(state: State, lock: asyncio.Lock):
    """Summarizes the previous user messages to minimize the number of token for the user input
       Args:
           state (State): the state of the constructed graph
           lock (asyncio.Lock): the lock object for the summarization purposes
       Returns:
           dict: the updates in the current state of the graph
    """
    thread_id = uuid.uuid4()
    config = {"configurable": {"thread_id": thread_id}}
    summary = state.get("summary", "")
    tool_node, model_with_tools, model = initialize_model(model_name)
    summary_message = SUMMARIZER_PROMPT
    if summary:
        summary_message += (f"This is summary of the conversation to date: {summary}\n\n"
                            "Extend the summary by taking into account the new messages above:")
    resolved_tool_call_ids = {msg.tool_call_id for msg in state["messages"] if isinstance(msg, ToolMessage)}
    valid_messages = [
        msg for msg in state["messages"]
        if not (hasattr(msg, "tool_calls") and any(call["id"] not in resolved_tool_call_ids for call in msg.tool_calls))
    ]

    messages = valid_messages + [SystemMessage(content=summary_message)]
    async with lock:
        response = await model_with_tools.ainvoke(messages, config)
    latest_messages_to_remove = [RemoveMessage(id=m.id) for m in state["messages"][:-2]]
    return {
        "summary": response.content,
        "messages": state["messages"][-2:],
        "remove_messages": latest_messages_to_remove,
    }


async def schedule_summarization(state: State, lock: asyncio.Lock):
    """Run summarization without blocking the main chatbot flow
       Args:
           state (State): the state of the constructed graph
           lock (asyncio.Lock): the lock object for the summarization purposes
    """
    asyncio.create_task(summarize_chat(state, lock))


def initialize_model(custom_model_name: str, temperature: str = "0"):
    """Initialize the model and bind it with the search engine
       Args:
           custom_model_name (str): the name of the desired OpenAI model
           temperature (str, default=0): the temperature of the desired model
       Returns:
           ToolNode: an instance of the ToolNode
           Runnable: an instance of ChatOpenAI model bind with the defined tool
           ChatOpenAI: an instance of ChatOpenAI model

    """
    model = ChatOpenAI(model_name=custom_model_name, temperature=model_temperature, api_key=openai_key)
    search = GoogleSearchAPIWrapper(google_api_key=google_api_key, google_cse_id=google_cse_id)
    tool = Tool(
        name="google_search",
        description="Search Google for recent results.",
        func=search.run,
    )
    tool_node = ToolNode(tools=[tool])
    model_with_tools = model.bind_tools([tool])
    return tool_node, model_with_tools, model


def initialize_app(tool_node: ToolNode, bot_lock: asyncio.Lock, sum_lock: asyncio.Lock):
    """Initializes the state graph that will be used to fulfill user's queries
       Args:
           tool_node (ToolNode): an instance of a ToolNode
           bot_lock (asyncio.Lock): a lock for direct user queries
           sum_lock (asyncio.Lock): a lock for direct user summarizer
       Returns:
           CompiledStateGraph: the state graph instance
    """

    async def chatbot_node(state, config):
        return await call_model(state, config, bot_lock)

    async def summarizer_node(state, config):
        await schedule_summarization(state, sum_lock)
        return state

    workflow = StateGraph(state_schema=State)
    workflow.add_node("chatbot", chatbot_node)
    workflow.add_node("tools", tool_node)
    workflow.add_node("chat_summary", summarizer_node)
    workflow.add_edge(START, "chatbot")
    workflow.add_conditional_edges(
        "chatbot",
        should_continue,
        ["tools", "chat_summary", END],
    )
    workflow.add_edge("tools", "chatbot")
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app


def should_continue(state: State):
    """Define if the engine should continue searching
       Args:
           state (State): the current state of the graph
       Returns:
           "tools": in case any tools need to be used for response
           "chat_summary": in case the content needs to be summarized
           END: in case the agent can stop
    """
    messages = state["messages"]
    last_message = messages[-1]
    if not last_message.tool_calls:
        if len(messages) > 16:
            return "chat_summary"
        return END
    else:
        return "tools"


async def run_app(user_input: str, thread_id: uuid, app: CompiledStateGraph):
    """Returns the content generated AI agent within the compiled state graph in streaming manner
       Args:
           user_input (str): user's query
           thread_id (str): the ID of the current thread to be invoked
           app (CompiledStateGraph): a state graph instance
       Yields:
           str: chunks of streamed content
    """
    configuration = {"configurable": {"thread_id": thread_id}}
    full_content = ""
    async for msg, metadata in app.astream(
            {"messages": [HumanMessage(content=user_input)]},
            configuration,
            stream_mode="messages"):
        if msg.content and isinstance(msg, AIMessageChunk):
            full_content += msg.content
            yield msg.content
