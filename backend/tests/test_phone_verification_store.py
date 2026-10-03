"""The store's own guarantees: expiry, atomic failure counting, single use."""

from app import phone_verification_store as store
from app.phone_verification_store import (
    claim_send_slot,
    consume_token,
    count_failure,
    issue_token,
    read_challenge,
    release_send_slot,
    reset_signup_state,
    store_challenge,
    token_was_spent,
)

PHONE = "01099998888"


def setup_function() -> None:
    reset_signup_state()


def test_a_challenge_expires_on_its_own():
    """The reason this moved out of Postgres: nobody has to delete it.

    The old phone_verifications row sat there until someone wrote a purge, and
    nobody did -- so every phone number that ever asked for a code stayed.
    """
    store_challenge(PHONE, "hashed", 300)

    assert read_challenge(PHONE) == {"code_hash": "hashed", "fail_count": "0"}
    assert store._client().ttl(f"{store._CODE_PREFIX}{PHONE}") == 300


def test_a_token_expires_on_its_own():
    issue_token(PHONE, "tokenhash", 600)

    assert store._client().ttl(f"{store._TOKEN_PREFIX}tokenhash") == 600


def test_failures_accumulate_atomically():
    store_challenge(PHONE, "hashed", 300)

    assert [count_failure(PHONE) for _ in range(3)] == [1, 2, 3]
    assert read_challenge(PHONE)["fail_count"] == "3"


def test_a_resent_code_gets_a_fresh_attempt_budget():
    store_challenge(PHONE, "first", 300)
    count_failure(PHONE)
    count_failure(PHONE)

    store_challenge(PHONE, "second", 300)

    assert read_challenge(PHONE) == {"code_hash": "second", "fail_count": "0"}


def test_issuing_a_token_retires_the_challenge():
    store_challenge(PHONE, "hashed", 300)

    issue_token(PHONE, "tokenhash", 600)

    assert read_challenge(PHONE) is None


def test_a_token_is_spendable_once_and_remembers_being_spent():
    issue_token(PHONE, "tokenhash", 600)

    assert consume_token("tokenhash", spent_ttl_sec=600) == PHONE
    assert consume_token("tokenhash", spent_ttl_sec=600) is None
    # The marker is what lets signup() answer "already used" rather than
    # "never existed" for a replayed token.
    assert token_was_spent("tokenhash") is True


def test_an_unknown_token_was_never_spent():
    assert consume_token("nosuchtoken", spent_ttl_sec=600) is None
    assert token_was_spent("nosuchtoken") is False


def test_the_send_slot_is_held_until_released():
    assert claim_send_slot(PHONE, 60) is True
    assert claim_send_slot(PHONE, 60) is False

    release_send_slot(PHONE)

    assert claim_send_slot(PHONE, 60) is True
