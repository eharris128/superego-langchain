"""
Inner Agent Implementation

Core functionality for processing inputs that have passed superego evaluation,
using tools, and generating responses back to the user.
"""

import asyncio
from typing import AsyncGenerator, Dict, List, Tuple, Any, Optional
from datetime import datetime
import uuid
import json

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field

from ..models import FlowStep, StreamChunk
from .commands import COMPLETE, NEEDS_TOOL, NEEDS_RESEARCH, NEEDS_REVIEW, ERROR, AWAITING_TOOL_CONFIRMATION
from .prompts import INNER_AGENT_PROMPT


class ToolUsage(BaseModel):
    """Record of tool usage"""
    tool_name: str = Field(description="Name of the tool")
    input: Any = Field(description="Input provided to the tool")
    output: Any = Field(description="Output returned by the tool")
    requires_confirmation: bool = Field(description="Whether this tool requires user confirmation", default=True)


class InnerAgentOutput(BaseModel):
    """Structured output from inner agent processing"""
    thinking: str = Field(description="Detailed reasoning process (not shown to user)")
    tool_usage: Optional[ToolUsage] = Field(description="Record of tool used, if any", default=None)
    agent_guidance: str = Field(description="Hidden context for the next agent")
    response: str = Field(description="Helpful response to the user")
    next_agent: Optional[str] = Field(description="Next agent to call, or null to end flow", default=None)


async def process_with_tools(
    llm: BaseChatModel,
    input_message: str,
    system_prompt: str,
    agent_guidance: str,
    agent_id: str,
    available_tools: Dict[str, callable] = None
) -> Tuple[str, Optional[Dict[str, Any]], str, Optional[str]]:
    """Process an input message using available tools
    
    Args:
        llm: Language model for processing
        input_message: The user's input
        system_prompt: The system prompt for this agent
        agent_guidance: Guidance from the superego agent
        agent_id: Identifier for this agent
        available_tools: Dict of tool name -> function
        
    Returns:
        Tuple of (response, tool_usage, agent_guidance, next_agent)
    """
    # Create output parser
    parser = PydanticOutputParser(pydantic_object=InnerAgentOutput)
    
    # Format available tools string
    tools_str = "No tools available."
    if available_tools:
        tools_str = "\n".join([f"- {name}" for name in available_tools.keys()])
    
    # Create prompt
    prompt = ChatPromptTemplate.from_template(
        INNER_AGENT_PROMPT + "\n\n" + parser.get_format_instructions()
    )
    
    # Format prompt
    messages = prompt.format_messages(
        system_prompt=system_prompt,
        input_message=input_message,
        agent_guidance=agent_guidance,
        agent_id=agent_id,
        available_tools=tools_str
    )
    
    # Call LLM
    response = await llm.ainvoke(messages)
    result = parser.parse(response.content)
    
    # Convert tool usage to dict if present
    tool_usage_dict = None
    if result.tool_usage:
        tool_usage_dict = result.tool_usage.dict()
    
    # Validate next_agent decision
    if result.next_agent == "self":
        result.next_agent = agent_id
    elif result.next_agent == "complete" or result.next_agent == "end":
        result.next_agent = None
    
    return result.response, tool_usage_dict, result.agent_guidance, result.next_agent


async def execute_tool(
    tool_name: str,
    tool_input: Any,
    available_tools: Dict[str, callable]
) -> Any:
    """Execute a tool and return its output
    
    Args:
        tool_name: Name of the tool to execute
        tool_input: Input to provide to the tool
        available_tools: Dict of available tools
        
    Returns:
        Tool execution result
    """
    if tool_name not in available_tools:
        return f"Error: Tool '{tool_name}' not found"
        
    tool_func = available_tools[tool_name]
    
    try:
        # Execute the tool (handle both sync and async tools)
        if asyncio.iscoroutinefunction(tool_func):
            result = await tool_func(tool_input)
        else:
            result = tool_func(tool_input)
        return result
    except Exception as e:
        return f"Error executing tool: {str(e)}"


async def create_inner_agent_step(
    prev_step: Dict[str, Any],
    agent_id: str,
    response: str,
    tool_usage: Optional[Dict[str, Any]],
    agent_guidance: str,
    thinking: str,
    system_prompt: str,
    next_agent: Optional[str]
) -> FlowStep:
    """Create an inner agent step
    
    Args:
        prev_step: Previous step in the flow
        agent_id: Identifier for this agent
        response: The agent's response
        tool_usage: Record of any tool usage
        agent_guidance: Guidance for the next agent
        thinking: Reasoning process
        system_prompt: System prompt for this agent
        next_agent: Next agent to call
        
    Returns:
        Complete FlowStep
    """
    # Create step
    return FlowStep(
        step_id=str(uuid.uuid4()),
        agent_id=agent_id,
        timestamp=datetime.now().isoformat(),
        role="assistant",
        input=prev_step.get("input", ""),
        system_prompt=system_prompt,
        thinking=thinking,
        tool_usage=tool_usage,
        agent_guidance=agent_guidance,
        response=response,
        next_agent=next_agent
    )


async def create_inner_agent_node(
    llm: BaseChatModel,
    agent_id: str,
    system_prompt: str,
    available_tools: Dict[str, callable] = None
) -> callable:
    """Create a langgraph-compatible inner agent node function
    
    Args:
        llm: The language model to use
        agent_id: Identifier for this agent
        system_prompt: System prompt for this agent
        available_tools: Dict of available tools
        
    Returns:
        Async generator function that streams results
    """
    if available_tools is None:
        available_tools = {}
        
    async def inner_agent_node(
        state: Dict[str, Any]
    ) -> AsyncGenerator[StreamChunk, None]:
        """Inner agent node function that processes inputs and streams results"""
        # Get the most recent message
        steps = state.get("steps", [])
        prev_step = steps[-1] if steps else {}
        
        # Get input and agent_guidance from previous step
        input_message = prev_step.get("input", "")
        agent_guidance = prev_step.get("agent_guidance", "")
        
        # Early yield of processing message
        yield StreamChunk(
            partial_output=f"Processing as {agent_id}...",
            complete=False
        )
        
        # Process the input
        response, tool_usage, new_guidance, next_agent = await process_with_tools(
            llm, input_message, system_prompt, agent_guidance, 
            agent_id, available_tools
        )
        
        # If tool usage is indicated
        if tool_usage and "tool_name" in tool_usage and tool_usage["tool_name"] in available_tools:
            tool_name = tool_usage["tool_name"]
            tool_input = tool_usage["input"]
            
            # Check if the flow instance has confirmation settings
            instance_id = state.get("instance_id")
            
            # Default to requiring confirmation
            requires_confirmation = True
            
            # Check if we can access the flow engine and confirmation settings
            from ..flow.engine import flow_engine
            if hasattr(flow_engine, 'active_flows') and instance_id in flow_engine.active_flows:
                flow_instance = flow_engine.active_flows[instance_id]
                
                # Get confirmation settings
                confirm_all = flow_instance.get("tool_confirmation_settings", {}).get("confirm_all", True)
                exempted_tools = flow_instance.get("tool_confirmation_settings", {}).get("exempted_tools", [])
                
                # Check if this tool is exempt from confirmation
                requires_confirmation = confirm_all and tool_name not in exempted_tools
                
                # Add this information to tool_usage
                tool_usage["requires_confirmation"] = requires_confirmation
                
                # If confirmation is required, save pending tool execution and yield awaiting message
                if requires_confirmation:
                    # Generate a unique ID for this tool execution
                    tool_execution_id = str(uuid.uuid4())
                    
                    # Store pending tool execution
                    flow_instance["pending_tool_executions"][tool_execution_id] = {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "state": state,
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    # Update response and next_agent to indicate waiting for confirmation
                    response = f"I'd like to use the tool '{tool_name}' with the following input:\n\n{json.dumps(tool_input, indent=2)}\n\nPlease confirm if I can proceed."
                    next_agent = AWAITING_TOOL_CONFIRMATION
                    
                    # Set tool_usage output to indicate awaiting confirmation
                    tool_usage["output"] = "Awaiting user confirmation"
                    
                    # Create thinking and update agent guidance
                    new_guidance += f"\nTool {tool_name} requires confirmation. Execution ID: {tool_execution_id}"
                    
                    # Yield message about awaiting confirmation
                    yield StreamChunk(
                        partial_output=f"Waiting for confirmation to use tool: {tool_name}...",
                        complete=False
                    )
                else:
                    # No confirmation needed, execute the tool
                    yield StreamChunk(
                        partial_output=f"Using tool: {tool_name}...",
                        complete=False
                    )
                    
                    tool_result = await execute_tool(tool_name, tool_input, available_tools)
                    
                    # Update tool usage with result
                    tool_usage["output"] = tool_result
                    
                    # If NEEDS_TOOL, self-loop to process tool result
                    if next_agent == agent_id or next_agent == "self":
                        # Create new agent guidance with tool result
                        new_guidance += f"\nTool {tool_name} returned: {str(tool_result)}"
            else:
                # Cannot access confirmation settings, default to execute
                yield StreamChunk(
                    partial_output=f"Using tool: {tool_name}...",
                    complete=False
                )
                
                tool_result = await execute_tool(tool_name, tool_input, available_tools)
                
                # Update tool usage with result
                tool_usage["output"] = tool_result
                
                # If NEEDS_TOOL, self-loop to process tool result
                if next_agent == agent_id or next_agent == "self":
                    # Create new agent guidance with tool result
                    new_guidance += f"\nTool {tool_name} returned: {str(tool_result)}"
        
        # Create thinking based on guidance and any tool usage
        thinking = (
            f"Processed input: {input_message}\n"
            f"With guidance: {agent_guidance}\n"
            f"Tool usage: {json.dumps(tool_usage) if tool_usage else 'None'}\n"
            f"Response: {response}"
        )
        
        # Create the step
        step = await create_inner_agent_step(
            prev_step, agent_id, response, tool_usage, 
            new_guidance, thinking, system_prompt, next_agent
        )
        
        # Create final streaming chunk with complete step
        yield StreamChunk(
            partial_output=step.response,
            complete=True,
            flow_step=step
        )
    
    return inner_agent_node
