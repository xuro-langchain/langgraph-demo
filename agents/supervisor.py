import ast
from typing_extensions import TypedDict
from typing import Annotated, Optional, List

from agents.utils import invoice_graph as invoice_agent
from react.music_agent import graph as music_agent
from utils import llm, get_engine_for_chinook_db

from langchain_community.utilities.sql_database import SQLDatabase
from langgraph.graph import StateGraph, START, END

from langgraph.graph.message import AnyMessage, add_messages
from langgraph.managed.is_last_step import RemainingSteps

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig


engine = get_engine_for_chinook_db()
db = SQLDatabase(engine)

supervisor_prompt = """You are an expert customer support assistant for a digital music store. 
You are dedicated to providing exceptional service and ensuring customer queries are answered thoroughly. 
You have a team of subagents that you can use to help answer queries from customers. 
Your primary role is to serve as a supervisor/planner for this multi-agent team that helps answer queries from customers. 

Your team is composed of two subagents that you can use to help answer the customer's request:
1. music_catalog_information_subagent: this subagent has access to user's saved music preferences. It can also retrieve information about the digital music store's music 
catalog (albums, tracks, songs, etc.) from the database. 
3. invoice_information_subagent: this subagent is able to retrieve information about a customer's past purchases or invoices 
from the database. 

Based on the existing steps that have been taken in the messages, your role is to generate the next subagent that needs to be called. 
This could be one step in an inquiry that needs multiple sub-agent calls. """


from langgraph_supervisor import create_supervisor

class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    customer_id: Optional[str]
    loaded_memory: Optional[str]
    remaining_steps: Optional[RemainingSteps]

# Create supervisor workflow
supervisor_prebuilt_workflow = create_supervisor(
    agents=[invoice_agent, music_agent],
    output_mode="last_message", # alternative is full_history
    model=llm,
    prompt=(supervisor_prompt), 
    state_schema=State
)

# ------------------------------------------------------------
# Adding HITL to the Supervisor
# ------------------------------------------------------------

from pydantic import BaseModel, Field

class UserInput(BaseModel):
    """Schema for parsing user-provided account information."""
    identifier: str = Field(description = "Identifier, which can be a customer ID, email, or phone number.")


structured_llm = llm.with_structured_output(schema=UserInput)
structured_system_prompt = """You are a customer service representative responsible for extracting customer identifier.\n 
Only extract the customer's account information from the message history. 
If they haven't provided the information yet, return an empty string for the file"""

from typing import Optional

# Helper 
def get_customer_id_from_identifier(identifier: str) -> Optional[int]:
    """
    Retrieve Customer ID using an identifier, which can be a customer ID, email, or phone number.
    
    Args:
        identifier (str): The identifier can be customer ID, email, or phone.
    
    Returns:
        Optional[int]: The CustomerId if found, otherwise None.
    """
    if identifier.isdigit():
        return int(identifier)
    elif identifier[0] == "+":
        query = f"SELECT CustomerId FROM Customer WHERE Phone = '{identifier}';"
        result = db.run(query)
        formatted_result = ast.literal_eval(result)
        if formatted_result:
            return formatted_result[0][0]
    elif "@" in identifier:
        query = f"SELECT CustomerId FROM Customer WHERE Email = '{identifier}';"
        result = db.run(query)
        formatted_result = ast.literal_eval(result)
        if formatted_result:
            return formatted_result[0][0]
    return None 


# Node

def verify_info(state: State, config: RunnableConfig):
    """Verify the customer's account by parsing their input and matching it with the database."""

    if state.get("customer_id") is None: 
        system_instructions = """You are a music store agent, where you are trying to verify the customer identity 
        as the first step of the customer support process. 
        Only after their account is verified, you would be able to support them on resolving the issue. 
        In order to verify their identity, one of their customer ID, email, or phone number needs to be provided.
        If the customer has not provided their identifier, please ask them for it.
        If they have provided the identifier but cannot be found, please ask them to revise it."""

        user_input = state["messages"][-1] 
    
        # Parse for customer ID
        parsed_info = structured_llm.invoke([SystemMessage(content=structured_system_prompt)] + [user_input])
    
        # Extract details
        identifier = parsed_info.identifier
    
        customer_id = None
        # Attempt to find the customer ID
        if (identifier):
            customer_id = get_customer_id_from_identifier(identifier)
    
        if customer_id is not None:
            intent_message = SystemMessage(
                content= f"Thank you for providing your information! I was able to verify your account with customer id {customer_id}."
            )
            return {
                "customer_id": str(customer_id),  # Convert to string for state
                "messages" : [intent_message]
            }
        else:
          response = llm.invoke([SystemMessage(content=system_instructions)]+state['messages'])
          return {"messages": [response]}
    else: 
        pass

from langgraph.types import interrupt
# Node
def human_input(state: State, config: RunnableConfig):
    """ No-op node that should be interrupted on """
    user_input = interrupt("Please provide input.")
    return {"messages": [user_input]}


# conditional_edge
def should_interrupt(state: State, config: RunnableConfig):
    if state.get("customer_id") is not None:
        return "continue"
    else:
        return "interrupt"

supervisor_prebuilt = supervisor_prebuilt_workflow.compile(name="music_catalog_subagent")

# ------------------------------------------------------------
# Adding Memory to the Supervisor
# ------------------------------------------------------------
from langgraph.store.base import BaseStore
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

# Initializing long term memory store 
in_memory_store = InMemoryStore()

# Initializing checkpoint for thread-level memory 
checkpointer = MemorySaver()

# helper function to structure memory 
def format_user_memory(user_data):
    """Formats music preferences from users, if available."""
    profile = user_data['memory']
    result = ""
    if hasattr(profile, 'music_preferences') and profile.music_preferences:
        result += f"Music Preferences: {', '.join(profile.music_preferences)}"
    return result.strip()

# Node
def load_memory(state: State, config: RunnableConfig, store: BaseStore):
    """Loads music preferences from users, if available."""
    
    user_id = state["customer_id"]
    namespace = ("memory_profile", user_id)
    existing_memory = store.get(namespace, "user_memory")
    formatted_memory = ""
    if existing_memory and existing_memory.value:
        formatted_memory = format_user_memory(existing_memory.value)

    return {"loaded_memory" : formatted_memory}

# User profile structure for creating memory

class UserProfile(BaseModel):
    customer_id: str = Field(
        description="The customer ID of the customer"
    )
    music_preferences: List[str] = Field(
        description="The music preferences of the customer"
    )


create_memory_prompt = """You are an expert analyst that is observing a conversation that has taken place between a customer and a customer support assistant. The customer support assistant works for a digital music store, and has utilized a multi-agent team to answer the customer's request. 
You are tasked with analyzing the conversation that has taken place between the customer and the customer support assistant, and updating the memory profile associated with the customer. The memory profile may be empty. If it's empty, you should create a new memory profile for the customer.

You specifically care about saving any music interest the customer has shared about themselves, particularly their music preferences to their memory profile.

To help you with this task, I have attached the conversation that has taken place between the customer and the customer support assistant below, as well as the existing memory profile associated with the customer that you should either update or create. 

The customer's memory profile should have the following fields:
- customer_id: the customer ID of the customer
- music_preferences: the music preferences of the customer

These are the fields you should keep track of and update in the memory profile. If there has been no new information shared by the customer, you should not update the memory profile. It is completely okay if you do not have new information to update the memory profile with. In that case, just leave the values as they are.

*IMPORTANT INFORMATION BELOW*

The conversation between the customer and the customer support assistant that you should analyze is as follows:
{conversation}

The existing memory profile associated with the customer that you should either update or create based on the conversation is as follows:
{memory_profile}

Ensure your response is an object that has the following fields:
- customer_id: the customer ID of the customer
- music_preferences: the music preferences of the customer

For each key in the object, if there is no new information, do not update the value, just keep the value that is already there. If there is new information, update the value. 

Take a deep breath and think carefully before responding.
"""

# Node
def create_memory(state: State, config: RunnableConfig, store: BaseStore):
    user_id = str(state["customer_id"])
    namespace = ("memory_profile", user_id)
    existing_memory = store.get(namespace, "user_memory")
    if existing_memory and existing_memory.value:
        existing_memory_dict = existing_memory.value
        formatted_memory = (
            f"Music Preferences: {', '.join(existing_memory_dict.get('music_preferences', []))}"
        )
    else:
        formatted_memory = ""
    formatted_system_message = SystemMessage(content=create_memory_prompt.format(conversation=state["messages"], memory_profile=formatted_memory))
    updated_memory = llm.with_structured_output(UserProfile).invoke([formatted_system_message])
    key = "user_memory"
    store.put(namespace, key, {"memory": updated_memory})


# Add nodes 
multi_agent = StateGraph(State)
multi_agent.add_node("verify_info", verify_info)
multi_agent.add_node("human_input", human_input)
multi_agent.add_node("load_memory", load_memory)
multi_agent.add_node("multiagent", supervisor_prebuilt)
multi_agent.add_node("create_memory", create_memory)

multi_agent.add_edge(START, "verify_info")
multi_agent.add_conditional_edges(
    "verify_info",
    should_interrupt,
    {
        "continue": "load_memory",
        "interrupt": "human_input",
    },
)
multi_agent.add_edge("human_input", "verify_info")
multi_agent.add_edge("load_memory", "multiagent")
multi_agent.add_edge("multiagent", "create_memory")
multi_agent.add_edge("create_memory", END)
# graph = multi_agent.compile(name="multiagent", checkpointer=checkpointer, store=in_memory_store)
graph = multi_agent.compile(name="assistant")