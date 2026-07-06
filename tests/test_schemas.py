"""Tests for request-body validation bounds on app/api/schemas.py."""

import pytest
from pydantic import ValidationError

from app.api.schemas import TicketCreate


def test_ticket_create_accepts_normal_input():
    ticket = TicketCreate(requester="hr@example.com", subject="Onboard employee", body="Onboard jdoe.")
    assert ticket.requester == "hr@example.com"


def test_ticket_create_rejects_empty_requester():
    with pytest.raises(ValidationError):
        TicketCreate(requester="", subject="s", body="b")


def test_ticket_create_rejects_requester_over_128_chars():
    with pytest.raises(ValidationError):
        TicketCreate(requester="x" * 129, subject="s", body="b")


def test_ticket_create_accepts_requester_at_128_chars():
    ticket = TicketCreate(requester="x" * 128, subject="s", body="b")
    assert len(ticket.requester) == 128


def test_ticket_create_rejects_subject_over_256_chars():
    with pytest.raises(ValidationError):
        TicketCreate(requester="r", subject="x" * 257, body="b")


def test_ticket_create_rejects_body_over_20000_chars():
    with pytest.raises(ValidationError):
        TicketCreate(requester="r", subject="s", body="x" * 20_001)


def test_ticket_create_accepts_body_at_20000_chars():
    ticket = TicketCreate(requester="r", subject="s", body="x" * 20_000)
    assert len(ticket.body) == 20_000
