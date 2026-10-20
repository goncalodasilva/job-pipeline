"""Pure URL/text rules for M4.3."""

from hypothesis import given
from hypothesis import strategies as st

from app.domain.job_intake import canonical_url
from app.domain.similarity import similarity


def test_canonical_url_drops_fragment_and_tracking_params():
    assert (
        canonical_url(
            "HTTPS://Jobs.Example.test:443/ad?utm_source=mail&id=7&fbclid=x&ref=feed#apply"
        )
        == "https://jobs.example.test/ad?id=7"
    )
    assert canonical_url("https://jobs.example.test/ad?b=2&a=1") == canonical_url(
        "https://jobs.example.test/ad?a=1&b=2"
    )


@given(st.text(min_size=1).filter(lambda x: any(c.isalnum() for c in x)), st.text())
def test_job_similarity_symmetric_bounded_one_for_same_text(a, b):
    assert 0 <= similarity(a, b) <= 1
    assert similarity(a, b) == similarity(b, a)
    assert similarity(a, a) == 1
