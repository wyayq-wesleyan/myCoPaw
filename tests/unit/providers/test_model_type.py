from qwenpaw.providers.model_type import infer_model_type


def test_infers_local_embedding_model():
    assert infer_model_type("Qwen3-Embedding-0.6B-8bit") == "embedding"


def test_infers_local_reranker_model():
    assert infer_model_type("Qwen3-Reranker-0.6B-4bit") == "reranker"


def test_keeps_regular_model_as_chat():
    assert infer_model_type("Ornith-1.0-35B-OptiQ-4bit") == "chat"
