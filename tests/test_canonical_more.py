"""Additional canonical_key cases (docs/02 §3). xfail = known gap in current impl."""
import pytest

from techradar.pipeline.canonical import canonical_key, normalize_url


def test_github_subpaths_merge_into_repo():
    base = canonical_key("https://github.com/o/r")
    for u in (
        "https://github.com/o/r/blob/main/README.md",
        "https://github.com/o/r/tree/dev",
        "https://github.com/O/R.git",
        "https://github.com/o/r/",
        "https://github.com/o/r?tab=readme-ov-file",
    ):
        assert canonical_key(u) == base, u


def test_github_releases_list_is_repo():
    assert canonical_key("https://github.com/o/r/releases") == ("gh:o/r", "repo")



def test_github_non_repo_paths_are_not_repos():
    for u in ("https://github.com/trending/python", "https://github.com/explore", "https://github.com/search?q=x",
              "https://github.com/collections/ai", "https://github.com/topics/llm"):
        k, kind = canonical_key(u)
        assert not k.startswith("gh:"), u



def test_github_issue_and_pr_are_not_the_repo():
    assert canonical_key("https://github.com/o/r/issues/12")[0] != "gh:o/r"
    assert canonical_key("https://github.com/o/r/pull/3")[0] != "gh:o/r"


def test_arxiv_new_id_variants():
    exp = ("arxiv:2408.12345", "paper")
    for u in ("https://arxiv.org/abs/2408.12345", "https://arxiv.org/abs/2408.12345v3", "https://arxiv.org/pdf/2408.12345v2.pdf",
              "https://arxiv.org/html/2408.12345v1", "http://export.arxiv.org/abs/2408.12345", "https://www.arxiv.org/abs/2408.12345"):
        assert canonical_key(u) == exp, u



def test_arxiv_old_id_format():
    a = canonical_key("https://arxiv.org/abs/hep-th/9901001")
    b = canonical_key("https://arxiv.org/pdf/hep-th/9901001v2")
    assert a == b and a[0].startswith("arxiv:") and a[1] == "paper"


def test_doi_variants_merge():
    a = canonical_key("https://doi.org/10.1145/3512345.3512346")
    b = canonical_key("http://dx.doi.org/10.1145/3512345.3512346")
    assert a == b == ("doi:10.1145/3512345.3512346", "paper")


def test_hf_dataset_and_model_distinct_and_subpaths_merge():
    m = canonical_key("https://huggingface.co/Org/Name")
    assert canonical_key("https://huggingface.co/org/name/tree/main") == m
    assert canonical_key("https://huggingface.co/models/org/name") == m
    assert canonical_key("https://huggingface.co/datasets/org/name") != m


def test_hf_non_model_paths_are_not_models():
    for u in ("https://huggingface.co/blog/foo", "https://huggingface.co/docs/transformers/index",
              "https://huggingface.co/collections/org/name-123"):
        assert not canonical_key(u)[0].startswith("hf:"), u



def test_hf_tasks_path_is_not_model():
    assert not canonical_key("https://huggingface.co/tasks/text-generation")[0].startswith("hf:")


def test_hn_item_with_and_without_www():
    assert canonical_key("https://news.ycombinator.com/item?id=42") == canonical_key("http://www.news.ycombinator.com/item?id=42")


def test_tracking_params_and_fragment_and_case_and_slash():
    a = canonical_key("HTTP://WWW.Example.COM/Path/?utm_campaign=x&fbclid=1&gclid=2&ref=hn&source=tw#frag")
    b = canonical_key("https://example.com/Path")
    assert a == b


def test_path_case_is_significant():
    assert canonical_key("https://example.com/Path") != canonical_key("https://example.com/path")


def test_query_order_insensitive():
    assert normalize_url("https://e.com/s?b=2&a=1") == normalize_url("https://e.com/s?a=1&b=2")



def test_prefix_filter_does_not_overmatch():
    assert normalize_url("https://e.com/a?refresh=1") != normalize_url("https://e.com/a")



def test_short_link_expansion_hook_exists():
    from techradar.pipeline import canonical
    assert hasattr(canonical, "expand_short_url"), "expected an expand step for bit.ly/t.co/... before hashing"



def test_default_port_stripped():
    assert normalize_url("https://example.com:443/x") == "https://example.com/x"
