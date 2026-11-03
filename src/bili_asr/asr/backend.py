"""Model execution port; workflow ownership and process lifetime stay outside it."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .strategies import RuntimeStrategies


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    precisions: tuple[str, ...]
    native_asr_batch: bool
    native_alignment_batch: bool
    cancellation_scope: str
    quantization_scopes: tuple[str, ...] = ()
    schema_version: int = 1
    attention_backends: tuple[str, ...] = ()
    static_cache: bool = False
    compiled_generation: bool = False
    compiled_alignment: bool = False

    def evidence(self) -> dict:
        return asdict(self)


class InferenceBackend(Protocol):
    capabilities: BackendCapabilities

    def generate(self, model: Any, inputs: Any, *, max_new_tokens: int,
                 disable_cache: bool) -> Any: ...

    def align(self, model: Any, inputs: Any) -> Any: ...


class HuggingFaceBackend:
    """Execute native Transformers calls in the existing task-owned process.

    This adapter does not own jobs, leases, retries, output commits, a second
    queue, or a shared engine. Aborting it means terminating its owned session;
    a future shared service must implement per-request abort before advertising
    a different cancellation scope.
    """

    capabilities = BackendCapabilities("huggingface", ("bfloat16", "float16"), True, True, "owned_process",
        attention_backends=("eager", "sdpa", "flash_attention_2"), static_cache=True,
        compiled_generation=True, compiled_alignment=True)

    def configure(self, config, models):
        self.strategies = RuntimeStrategies(config)
        self.strategies.prepare_models(models)

    def execution_evidence(self):
        strategies = getattr(self, "strategies", None)
        return strategies.evidence() if strategies else None

    def release(self):
        strategies = getattr(self, "strategies", None)
        if strategies:
            strategies.release()
        self.strategies = None

    def generate(self, model: Any, inputs: Any, *, max_new_tokens: int,
                 disable_cache: bool) -> Any:
        if getattr(self, "strategies", None) is not None:
            return self.strategies.generate(model, inputs, max_new_tokens=max_new_tokens, disable_cache=disable_cache)
        return model.generate(**inputs, max_new_tokens=max_new_tokens,
                              **({"use_cache": False} if disable_cache else {}))

    def align(self, model: Any, inputs: Any) -> Any:
        if getattr(self, "strategies", None) is not None:
            return self.strategies.align(model, inputs)
        return model(**inputs).logits
