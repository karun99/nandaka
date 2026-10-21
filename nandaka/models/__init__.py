"""GGUF model layer: registry, parsing, llama-server management, clients."""

from .gguf import classify_tier, read_gguf, verify_gguf  # noqa: F401
from .registry import ModelRegistry, tier_gate, feature_state  # noqa: F401
from .client import ChatClient, ChatError  # noqa: F401
from .server import LlamaServer, ModelManager, cloud_client  # noqa: F401
