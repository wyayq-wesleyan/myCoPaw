# -*- coding: utf-8 -*-
"""Shared fixtures for provider tests.

``ProviderManager._init_builtins`` is an intentional no-op for offline
deployments (official cloud providers must not be reachable), so tests that
exercise builtin-provider behavior re-register the builtins they need via
the :func:`register_builtins` fixture factory below. Registration goes
through the supported-in-source ``ProviderManager._add_builtin`` hook.
"""

from __future__ import annotations

import pytest

import qwenpaw.providers.provider_manager as provider_manager_module
from qwenpaw.providers.provider_manager import (
    PROVIDER_ALIYUN_CODINGPLAN,
    PROVIDER_ALIYUN_CODINGPLAN_INTL,
    PROVIDER_ALIYUN_TOKENPLAN,
    PROVIDER_ALIYUN_TOKENPLAN_INTL,
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE_OPENAI,
    PROVIDER_DASHSCOPE,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_GITHUB_MODELS,
    PROVIDER_KILO,
    PROVIDER_KIMI_CN,
    PROVIDER_KIMI_CODINGPLAN,
    PROVIDER_KIMI_INTL,
    PROVIDER_LMSTUDIO,
    PROVIDER_MINIMAX,
    PROVIDER_MINIMAX_CN,
    PROVIDER_MIMO_TOKENPLAN,
    PROVIDER_MODELSCOPE,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    PROVIDER_OPENCODE,
    PROVIDER_OPENROUTER,
    PROVIDER_OPENAI_RESPONSE,
    PROVIDER_QWENPAW,
    PROVIDER_SILICONFLOW_CN,
    PROVIDER_SILICONFLOW_INTL,
    PROVIDER_VOLCENGINE_CN,
    PROVIDER_VOLCENGINE_CN_CODINGPLAN,
    PROVIDER_ZHIPU_CN,
    PROVIDER_ZHIPU_CN_CODINGPLAN,
    PROVIDER_ZHIPU_INTL,
    PROVIDER_ZHIPU_INTL_CODINGPLAN,
    ProviderManager,
)

# Module-level builtin definitions keyed by provider id, mirroring the set
# ``_init_builtins`` registered before offline-only deployments.
BUILTIN_PROVIDER_DEFS = {
    PROVIDER_QWENPAW.id: PROVIDER_QWENPAW,
    PROVIDER_OLLAMA.id: PROVIDER_OLLAMA,
    PROVIDER_LMSTUDIO.id: PROVIDER_LMSTUDIO,
    PROVIDER_OPENROUTER.id: PROVIDER_OPENROUTER,
    PROVIDER_GITHUB_MODELS.id: PROVIDER_GITHUB_MODELS,
    PROVIDER_MODELSCOPE.id: PROVIDER_MODELSCOPE,
    PROVIDER_DASHSCOPE.id: PROVIDER_DASHSCOPE,
    PROVIDER_ALIYUN_CODINGPLAN.id: PROVIDER_ALIYUN_CODINGPLAN,
    PROVIDER_ALIYUN_CODINGPLAN_INTL.id: PROVIDER_ALIYUN_CODINGPLAN_INTL,
    PROVIDER_ALIYUN_TOKENPLAN.id: PROVIDER_ALIYUN_TOKENPLAN,
    PROVIDER_ALIYUN_TOKENPLAN_INTL.id: PROVIDER_ALIYUN_TOKENPLAN_INTL,
    PROVIDER_OPENCODE.id: PROVIDER_OPENCODE,
    PROVIDER_KILO.id: PROVIDER_KILO,
    PROVIDER_OPENAI.id: PROVIDER_OPENAI,
    PROVIDER_OPENAI_RESPONSE.id: PROVIDER_OPENAI_RESPONSE,
    PROVIDER_AZURE_OPENAI.id: PROVIDER_AZURE_OPENAI,
    PROVIDER_MINIMAX.id: PROVIDER_MINIMAX,
    PROVIDER_MINIMAX_CN.id: PROVIDER_MINIMAX_CN,
    PROVIDER_KIMI_CN.id: PROVIDER_KIMI_CN,
    PROVIDER_KIMI_INTL.id: PROVIDER_KIMI_INTL,
    PROVIDER_KIMI_CODINGPLAN.id: PROVIDER_KIMI_CODINGPLAN,
    PROVIDER_ZHIPU_CN.id: PROVIDER_ZHIPU_CN,
    PROVIDER_ZHIPU_CN_CODINGPLAN.id: PROVIDER_ZHIPU_CN_CODINGPLAN,
    PROVIDER_ZHIPU_INTL.id: PROVIDER_ZHIPU_INTL,
    PROVIDER_ZHIPU_INTL_CODINGPLAN.id: PROVIDER_ZHIPU_INTL_CODINGPLAN,
    PROVIDER_DEEPSEEK.id: PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI.id: PROVIDER_GEMINI,
    PROVIDER_ANTHROPIC.id: PROVIDER_ANTHROPIC,
    PROVIDER_SILICONFLOW_CN.id: PROVIDER_SILICONFLOW_CN,
    PROVIDER_SILICONFLOW_INTL.id: PROVIDER_SILICONFLOW_INTL,
    PROVIDER_VOLCENGINE_CN.id: PROVIDER_VOLCENGINE_CN,
    PROVIDER_VOLCENGINE_CN_CODINGPLAN.id: (
        PROVIDER_VOLCENGINE_CN_CODINGPLAN
    ),
    PROVIDER_MIMO_TOKENPLAN.id: PROVIDER_MIMO_TOKENPLAN,
}


@pytest.fixture
def register_builtins(monkeypatch):
    """Factory fixture that seeds builtin providers into new managers.

    Usage::

        def test_something(register_builtins):
            register_builtins("openai", "anthropic")
            manager = ProviderManager()  # now carries the seeded builtins

    Works by patching ``ProviderManager._init_builtins`` on the class, so
    every manager constructed during the test registers the requested
    builtins before its storage init runs, and monkeypatch restores the
    offline no-op at teardown.
    """

    def _register(*provider_ids: str) -> None:
        unknown = [
            pid for pid in provider_ids
            if pid not in BUILTIN_PROVIDER_DEFS
        ]
        if unknown:
            raise KeyError(f"Unknown builtin providers: {unknown}")

        def _init_builtins(self) -> None:  # noqa: ANN001
            for provider_id in provider_ids:
                self._add_builtin(BUILTIN_PROVIDER_DEFS[provider_id])

        monkeypatch.setattr(
            ProviderManager, "_init_builtins", _init_builtins
        )

    return _register
