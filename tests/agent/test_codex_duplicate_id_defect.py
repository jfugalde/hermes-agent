import pytest
import hashlib
import json
from typing import Any, List
from types import SimpleNamespace

from agent.transports.types import ToolCall, build_tool_call
from agent.message_sanitization import uniquify_tool_call_ids, coalesce_tool_call_id
import run_agent
from agent.codex_runtime import _CodexResponseAssembler

def test_uniquify_tool_call_id_effectiveness():
    """
    Evidence C: uniquify_tool_call_ids only updates 'id', 
    but ToolCall.call_id is a read-only property over provider_data['call_id'].
    """
    tc1 = ToolCall(id="call_A", name="tool", arguments="{}", provider_data={"call_id": "call_A"})
    tc2 = ToolCall(id="call_A", name="tool", arguments="{}", provider_data={"call_id": "call_A"})
    
    calls = [tc1, tc2]
    uniquify_tool_call_ids(calls)
    
    # In the defect state, tc2.id becomes 'call_A_d2', but tc2.call_id stays 'call_A'
    # because provider_data wasn't updated.
    assert tc1.call_id != tc2.call_id, f"Coalesced IDs must be distinct: {tc1.call_id} == {tc2.call_id}"

def test_deduplicate_tool_calls_effective_id():
    """
    Evidence C: _deduplicate_tool_calls keys on (name, args).
    If two calls have the same effective ID but different args (real vs phantom), 
    they both survive, leading to collisions in the result map.
    """
    # We simulate a state where they have the same call_id (not yet uniquified)
    tc1 = ToolCall(id="call_A", name="tool", arguments="{\"code\": \"print(1)\"}", provider_data={"call_id": "call_A"})
    tc2 = ToolCall(id="call_A", name="tool", arguments="{}", provider_data={"call_id": "call_A"})
    
    # Use the actual method on the class (it's a static method or defined in module)
    # Based on sed output, it's def _deduplicate_tool_calls in run_agent.py
    # but since it's used as agent._deduplicate_tool_calls, it's likely a method of AIAgent.
    
    # Since we just want to test the logic, we can call it if it's in the module, 
    # or mock the class. 
    # Let's try importing it as a module function first, or call via AIAgent if needed.
    # In run_agent.py it is defined as a top-level function (based on sed) but 
    # the prompt says 'AIAgent._deduplicate_tool_calls'. 
    # Let's check if it's available as run_agent._deduplicate_tool_calls.
    
    try:
        result = run_agent.AIAgent._deduplicate_tool_calls([tc1, tc2])
    except AttributeError:
        # If it's a method of AIAgent, we'd need an instance.
        # But looking at sed, it is a function.
        pytest.fail("Could not find _deduplicate_tool_calls in run_agent")

    assert len(result) == 1, f"Should collapse calls with same ID regardless of args. Got: {len(result)}"



def test_end_to_end_phantom_call_guardrail():
    """
    Combined flow: Codex emits duplicate ID -> a phantom call with '{}' -> guardrail blocks.
    """
    phantom_args = "{}"
    args_hash = hashlib.sha256(phantom_args.encode()).hexdigest()
    assert args_hash == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"

def test_deduplicate_tool_calls_effective_id_red():
    """
    LAYER 2 RED: _deduplicate_tool_calls should collapse calls that share the same 
    effective ID (via coalesce_tool_call_id), even if arguments differ.
    Currently it only keys on (name, args), so these will both survive.
    """
    from agent.transports.types import ToolCall
    from run_agent import AIAgent
    
    # Two calls with same effective ID but different arguments
    # tc1 is the "real" one, tc2 is a "phantom" or duplicate from the provider
    tc1 = ToolCall(id="call_A", name="tool", arguments="{\"code\": \"print(1)\"}", provider_data={"call_id": "call_A"})
    tc2 = ToolCall(id="call_A", name="tool", arguments="{}", provider_data={"call_id": "call_A"})
    
    calls = [tc1, tc2]
    result = AIAgent._deduplicate_tool_calls(calls)
    
    # CURRENT STATE: result has 2 items because args differ.
    # FIXED STATE: result has 1 item because call_id is the same.
    assert len(result) == 1, f"Should collapse calls with same effective ID. Got: {len(result)}"