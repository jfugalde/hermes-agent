
import pytest
from agent.transports.types import ToolCall
from agent.message_sanitization import uniquify_tool_call_ids, coalesce_tool_call_id

def test_uniquify_tool_call_objects_failure():
    """
    REPRO: uniquify_tool_call_ids renames .id but not provider_data['call_id'].
    coalesce_tool_call_id prefers 'call_id' over 'id', so it returns the same ID.
    """
    # Create two ToolCall objects with same call_id
    tc1 = ToolCall(id="call_A", name="test", arguments="{}", provider_data={"call_id": "call_A"})
    tc2 = ToolCall(id="call_A", name="test", arguments="{}", provider_data={"call_id": "call_A"})
    
    calls = [tc1, tc2]
    uniquify_tool_call_ids(calls)
    
    # The .id of tc2 should be renamed to call_A_d2
    assert tc2.id == "call_A_d2"
    
    # BUT coalesce_tool_call_id still returns "call_A" because provider_data['call_id'] is unchanged
    # This is the defect.
    cid1 = coalesce_tool_call_id(tc1)
    cid2 = coalesce_tool_call_id(tc2)
    
    # In the current (broken) state, these are equal.
    # The fix should make them different.
    assert cid1 != cid2, f"Coalesced IDs should be different after uniquify, but both are {cid1}"

if __name__ == "__main__":
    try:
        test_uniquify_tool_call_objects_failure()
        print("Test PASSED (Defect FIXED)")
    except AssertionError as e:
        print(f"Test FAILED (Defect PRESENT): {e}")
