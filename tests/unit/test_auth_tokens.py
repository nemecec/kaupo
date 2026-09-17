"""Token roles: settings validation and check_token, without a database."""

# ruff: noqa: S106 (test tokens, not secrets)

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from kaupo.api.deps import Principal, check_token, require_admin, require_research
from kaupo.config import Settings


def _settings(**tokens: str) -> Settings:
    # _env_file=None: never read a local .env; explicit values override the environment
    values = {"admin_token": "", "readonly_token": "", "research_token": "", **tokens}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "tokens",
    [
        {"admin_token": "same", "readonly_token": "same"},
        {"admin_token": "same", "research_token": "same"},
        {"readonly_token": "same", "research_token": "same"},
        {"admin_token": "same", "readonly_token": "same", "research_token": "same"},
    ],
)
def test_duplicate_token_values_are_rejected(tokens: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="must differ"):
        _settings(**tokens)


def test_empty_tokens_do_not_count_as_duplicates() -> None:
    settings = _settings(admin_token="a")
    assert not settings.auth_disabled


def test_auth_disabled_only_when_every_token_is_absent() -> None:
    assert _settings().auth_disabled
    assert not _settings(research_token="r").auth_disabled
    assert not _settings(readonly_token="ro").auth_disabled
    assert not _settings(admin_token="a").auth_disabled


def test_check_token_maps_each_value_to_one_role() -> None:
    settings = _settings(admin_token="a", readonly_token="ro", research_token="r")

    admin = check_token("a", settings)
    assert admin is not None and admin.admin and admin.research

    research = check_token("r", settings)
    assert research is not None and not research.admin and research.research

    readonly = check_token("ro", settings)
    assert readonly is not None and not readonly.admin and not readonly.research

    assert check_token("", settings) is None
    assert check_token("nope", settings) is None


def test_research_only_deployment_rejects_other_tokens() -> None:
    # a research token alone enables auth: an unknown or empty token is refused
    settings = _settings(research_token="r")
    assert check_token("", settings) is None
    assert check_token("a", settings) is None


def test_disabled_auth_is_admin() -> None:
    principal = check_token("", _settings())
    assert principal is not None and principal.admin and principal.research


def test_principal_admin_keyword_stays_compatible() -> None:
    assert Principal(admin=True).research
    assert not Principal(admin=False).research


def test_require_dependencies() -> None:
    admin, research, readonly = (
        Principal(admin=True),
        Principal(admin=False, research=True),
        Principal(admin=False),
    )
    assert require_research(admin) is admin
    assert require_research(research) is research
    with pytest.raises(HTTPException) as exc:
        require_research(readonly)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        require_admin(research)
    assert exc.value.status_code == 403
