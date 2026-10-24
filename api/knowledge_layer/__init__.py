"""
Knowledge layer — prepared agent context for PropToIt.

Shifts retrieval from "find similar text" to "assemble task-shaped context."
"""
from knowledge_layer.models import ContextBundle, RetrievalContract, SourceProvenance
from knowledge_layer.assembler import ContextAssembler
from knowledge_layer.cache import CompiledContextCache
from knowledge_layer.memory import AgentMemory

__all__ = [
    "ContextBundle",
    "RetrievalContract",
    "SourceProvenance",
    "ContextAssembler",
    "CompiledContextCache",
    "AgentMemory",
]
