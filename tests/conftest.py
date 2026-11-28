import pytest

from glyde_mm import client


@pytest.fixture(autouse=True)
def unpaced_order_entries(request, monkeypatch):
    """Offline venues admit everything; tests marked ``paced`` keep Glyde's entry buckets."""
    if request.node.get_closest_marker("paced") is None:
        monkeypatch.setattr(client, "ORDER_ENTRY_RATES", {})
