import pytest

from docgen.errors import AuthError, TicketNotFoundError
from docgen.models import GeneratedDocument, Ticket, TokenUsage
from docgen.pipeline import Pipeline


class FakeJira:
    def get_ticket(self, key):
        if key == "DEMO-404":
            raise TicketNotFoundError("Ticket DEMO-404 was not found")
        if key == "DEMO-401":
            raise AuthError("Jira rejected the credentials")
        return Ticket(key=key, title=f"Title {key}")


class FakeBackend:
    name = "fake"

    def generate(self, ticket):
        return GeneratedDocument(ticket_key=ticket.key, title=ticket.title, body="## Body",
                                 backend="fake",
                                 usage=TokenUsage(model="m", prompt_tokens=1_000_000,
                                                  completion_tokens=1_000_000,
                                                  total_tokens=2_000_000))


class FakePublisher:
    name = "fake"

    def __init__(self):
        self.published = []

    def publish(self, document, *, public=False):
        self.published.append(document.ticket_key)
        return f"https://docs.test/{document.ticket_key}"


def make_pipeline(tmp_path, **kwargs):
    p = Pipeline(output_dir=tmp_path, price_in=1.0, price_out=2.0, **kwargs)
    p.__dict__.update(jira=FakeJira(), backend=FakeBackend(), publisher=FakePublisher())
    return p


def test_saves_document_and_reports_cost(tmp_path):
    [result] = make_pipeline(tmp_path).run(["DEMO-1"])
    assert result.ok and result.cost == pytest.approx(3.0)
    assert (tmp_path / "DEMO-1_document.md").read_text() == "# Title DEMO-1\n\n## Body"


def test_one_bad_ticket_does_not_stop_the_batch(tmp_path):
    pipeline = make_pipeline(tmp_path, publish="gdocs")
    results = pipeline.run(["DEMO-1", "DEMO-404", "DEMO-3"])
    assert [r.ok for r in results] == [True, False, True]
    assert "not found" in results[1].error
    assert pipeline.publisher.published == ["DEMO-1", "DEMO-3"]


def test_auth_failure_aborts_the_batch(tmp_path):
    with pytest.raises(AuthError):
        make_pipeline(tmp_path).run(["DEMO-401", "DEMO-2"])


def test_review_without_tty_never_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    pipeline = make_pipeline(tmp_path, publish="gdocs", review=True)
    [result] = pipeline.run(["DEMO-1"])
    assert result.ok and result.url == ""
    assert pipeline.publisher.published == []
