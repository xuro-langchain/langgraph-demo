from agents.utils import get_langgraph_docs_retriever, llm
from langchain.schema import Document
from typing import List
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END

retriever = get_langgraph_docs_retriever()

class GraphState(TypedDict):
    """
    Attributes:
        question: The user's question
        generation: The LLM's generation
        documents: List of helpful documents retrieved by the RAG pipeline
    """
    input: str
    output: str

class InputState(TypedDict):
    question: str

from langchain_core.messages import HumanMessage

def lowercase(state: GraphState):
    """
    Args:
        state (dict): The current graph state
    Returns:
        state (dict): New key added to state, output 
    """
    input = state["input"]
    return {"output": input.lower()}


def capitalize(state: GraphState):
    """
    Args:
        state (dict): The current graph state
    Returns:
        state (dict): Output modified 
    """
    output = state["output"]
    return {"output": output.capitalize() }

graph_builder = StateGraph(GraphState, input=InputState)
graph_builder.add_node("capitalize", capitalize)
graph_builder.add_node("lowercase", lowercase)
graph_builder.add_edge(START, "lowercase")
graph_builder.add_edge("lowercase", "capitalize")
graph_builder.add_edge("capitalize", END)

graph = graph_builder.compile()