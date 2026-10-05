
import pytest
import json
from agent.transports.types import ToolCall
from run_agent import AIAgent
from agent.message_sanitization import coalesce_tool_call_id, uniquify_tool_call_ids

def test_deduplicate_tool_calls_by_effective_id():
    """
    REPRO: _deduplicate_tool_calls currently keys on (name, args).
    If two tool calls share the same effective ID but have different args 
    (one real, one phantom '{}'), they both survive.
    The fix should key on the coalesced ID.
    """
    # Two calls with same call_id but different args
    tc1 = ToolCall(id="call_A", name="test", arguments="{'code': 'print(1)'}", provider_data={"call_id": "call_A"})
    tc2 = ToolCall(id="call_A", name="test", arguments="{}", provider_data={"call_id": "call_A"})
    
    calls = [tc1, tc2]
    
    # Current broken behavior: (test, '{"code": "print(1)"}') != (test, '{}'), so both stay.
    # Fixed behavior: both share call_A, so only the first stays.
    
    unique = AIAgent._deduplicate_tool_calls(calls)
    
    assert len(unique) == 1, f"Expected 1 unique call (keyed by ID), got {len(unique)}"
    assert unique[0] == tc1

if __name__ == "__main__":
    try:
        test_deduplicate_tool_calls_by_effective_id()
        print("Test PASSED (Defect FIXED)")
    except AssertionError as e:
        print(f"Test FAILED (Defect PRESENT): {e}")
