import os
import pytest
from app.core.config import settings
from app.llm.openai_client import get_llm_client, TestDoubleLLMClient, OpenAILLMClient


def test_test_environment_blocks_implicit_real_llm_client():
    """Verify that calling get_llm_client('openai') under test environment raises RuntimeError."""
    with pytest.raises(RuntimeError, match="External live LLM API access via get_llm_client"):
        get_llm_client("openai")


def test_test_environment_defaults_to_test_double():
    """Verify that default test environment settings resolve to TestDoubleLLMClient."""
    assert settings.LLM_PROVIDER == "test_double"
    client = get_llm_client()
    assert isinstance(client, TestDoubleLLMClient)


def test_test_double_state_is_cleaned_between_tests():
    """Verify that test double scripted responses are empty by default."""
    client = get_llm_client("test_double")
    assert isinstance(client, TestDoubleLLMClient)
    assert getattr(client, "_scripted_responses", []) == []


def test_external_dns_interception_blocks_unauthorized_domains():
    """Verify that socket getaddrinfo maps external LLM providers to deterministic mock IPs."""
    import socket
    addr = socket.getaddrinfo("api.deepseek.com", 443)
    # Must be intercepted by _deterministic_test_getaddrinfo
    assert addr[0][4][0] == "93.184.216.34"
