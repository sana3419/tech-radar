from techradar.pipeline.canonical import canonical_key, normalize_url


def test_github_repo_variants_merge():
    a = canonical_key("https://github.com/vllm-project/vllm")
    b = canonical_key("http://www.github.com/VLLM-Project/vllm.git/")
    c = canonical_key("https://github.com/vllm-project/vllm/tree/main/docs?utm_source=x")
    assert a == b == c == ("gh:vllm-project/vllm", "repo")


def test_github_release():
    k, kind = canonical_key("https://github.com/ggml-org/llama.cpp/releases/tag/b1234")
    assert k == "gh:ggml-org/llama.cpp#tag/b1234" and kind == "release"


def test_arxiv_versions_merge():
    a = canonical_key("https://arxiv.org/abs/2408.12345v2")
    b = canonical_key("https://arxiv.org/pdf/2408.12345")
    c = canonical_key("https://huggingface.co/papers/2408.12345")
    assert a == b == c == ("arxiv:2408.12345", "paper")


def test_hf_model():
    assert canonical_key("https://huggingface.co/Qwen/Qwen3-8B") == ("hf:models/qwen/qwen3-8b", "repo")


def test_hn_text_post():
    assert canonical_key("https://news.ycombinator.com/item?id=123") == ("hn:123", "post")


def test_generic_url_strips_tracking():
    a = canonical_key("https://Example.com/blog/post/?utm_source=hn&ref=tw#top")
    b = canonical_key("https://example.com/blog/post")
    assert a == b and a[1] == "article"


def test_normalize_keeps_meaningful_query():
    assert normalize_url("http://example.com/s?q=rust&utm_medium=x") == "https://example.com/s?q=rust"
