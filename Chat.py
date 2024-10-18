import streamlit as st
import asyncio
import uuid
import nest_asyncio
from dotenv import dotenv_values
from langchain_core.messages import HumanMessage, AIMessage
from chatbot import run_app, initialize_model, initialize_app

# Prevent nested event loops
nest_asyncio.apply()

# Initialize environmental variables
configs = dotenv_values()
model_name = configs["MODEL_NAME"]


class StreamHandler:
    """The handler streaming output of the generated content"""
    def __init__(self, container):
        """Initiate the message attributes
           Args:
                container (Generator): the container the content should be output
        """
        self.container = container
        self.buffer = ""
        self.last_token = ""

    def update(self, token: str):
        """Update the container with the latest token chunk. Takes care of proper output in cse chunks ends in the
           middle of the word.
           Args:
               token (str): the current generated token chunk in the content
           Returns:
               buffer (str): the current state of the message buffer
        """
        if self.last_token and not self.last_token.endswith(" "):
            self.buffer += token
        else:
            self.buffer += " " + token.lstrip()

        self.container.markdown(self.buffer)
        self.last_token = token
        return self.buffer


def create_locks():
    """Create new locks to the current event loop."""
    return asyncio.Lock(), asyncio.Lock()


async def stream_chat():
    """Output the user and agent messages in the dedicated containers, in streaming manner and chat-like form.
       Update the history of the chat.
    """

    # Outputs the user input and save it in the chat history
    with st.chat_message("user"):
        st.markdown(query)
    st.session_state.chat_history.append(HumanMessage(content=query))

    # Outputs the agent response in streaming manner and save it in the chat history
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            chat_box = st.empty()
            handler = StreamHandler(chat_box)
            response = ""

            async for chunk in run_app(
                    user_input=query,
                    thread_id=st.session_state.thread_id,
                    app=st.session_state.app,
            ):
                response = handler.update(chunk)

            st.session_state.chat_history.append(AIMessage(content=response))


def run_async_function(function):
    """Run an async function within Streamlit to avoid coroutine conflicts in the event loops.
       Args:
           function (Callable): a function to run inside the event loop

    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(function)
    finally:
        loop.close()


# Initialize the page configuration
st.set_page_config(page_title="Yet Another ChatBot", page_icon="🦾")
st.title("✨Yet another ChatBot✨")

# Initialize state variables
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "bot_lock" not in st.session_state:
    st.session_state.bot_lock = asyncio.Lock()

if "sum_lock" not in st.session_state:
    st.session_state.sum_lock = asyncio.Lock()

# if "executor" not in st.session_state:
#     st.session_state.executor = concurrent.futures.ThreadPoolExecutor()

if "app" not in st.session_state:
    bot_lock, sum_lock = create_locks()
    tool_node, model_with_tools, model = initialize_model(model_name)
    app = initialize_app(tool_node, st.session_state.bot_lock, st.session_state.sum_lock)
    st.session_state.app = app

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []


# Output chat view
chat_history_container = st.container()

with chat_history_container:
    for message in st.session_state.chat_history:
        if isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.write(message.content)
        elif isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.write(message.content)


# Require user input and generate response to it
query = st.chat_input(placeholder="How can I help you today?", key="user_input")

if query:
    run_async_function(stream_chat())
