"""Read Kimi's configured model aliases for `model` and `reasoning_effort` discovery.

Unlike Codex, which publishes a models cache keyed by provider slug, kimi's `-m` takes an
ALIAS the user defined in their own config.toml. The catalog therefore comes from
`kimi provider list --json`, which reports exactly the aliases that will work.

**This source contains secrets.** Its `providers` block carries `apiKey` in plaintext and
`baseUrl`, which can name private infrastructure. Neither is parsed into the catalog and
neither may ever reach an envelope — only alias names and their effort metadata are read.
`parse_catalog` is deliberately allowlist-shaped (it reads named fields off `models`) rather
than filtering a copy of the payload, so a new secret-bearing field upstream cannot leak by
default.

Unlike the Codex catalog, this one is AUTHORITATIVE for the alias set: kimi rejects an
unknown alias outright (`invalid_model`). It is only advisory for whether a given effort is
honoured — see `supported_efforts_for`.
"""

from __future__ import annotations

import json

from pontonier.core import runtime

from moonbridge import cli_contract
from moonbridge.schemas import ModelCatalogResult, ModelInfo

_ADVISORY = (
    "Model aliases configured in your kimi config.toml. `model` takes one of these alias "
    "names, not a raw provider model id. supported_reasoning_efforts is what the alias "
    "declares; kimi does not reject an unrecognized effort, it ignores it."
)
_UNAVAILABLE = (
    "No model catalog: `kimi provider list --json` returned nothing usable. Run "
    '`kimi login` or define a provider and a [models."<alias>"] entry in config.toml, '
    "then rerun kimi_status."
)

PROBE_TIMEOUT_SECONDS = 10


def _effort_token(value: object) -> str | None:
    """An effort token, or None when it fails the defensive shape."""
    if not isinstance(value, str) or not cli_contract.REASONING_EFFORT_TOKEN_PATTERN.match(value):
        return None
    return value


def _supported_efforts(raw: object) -> list[str] | None:
    """Effort tokens from an alias's `supportEfforts`, or None when absent/unusable.

    None = absent or unusable; [] = an explicitly empty advertised set. The distinction
    matters to `supported_efforts_for`, which must not treat "unknown" as "none allowed".
    """
    if not isinstance(raw, list):
        return None
    efforts: list[str] = []
    for entry in raw[: cli_contract.SUPPORTED_EFFORTS_MAX_ENTRIES]:
        token = _effort_token(entry)
        if token is not None and token not in efforts:
            efforts.append(token)
    if raw and not efforts:
        return None
    return efforts


def parse_catalog(payload: object) -> list[ModelInfo] | None:
    """Model aliases from a `kimi provider list --json` payload, or None if it drifted.

    Reads ONLY the `models` map, and only named fields within it. The sibling `providers`
    map (apiKey, baseUrl) is never touched.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("models")
    if not isinstance(entries, dict):
        return None
    models: list[ModelInfo] = []
    for alias, entry in list(entries.items())[: cli_contract.MODELS_CACHE_MAX_ENTRIES]:
        if not isinstance(alias, str) or not cli_contract.MODEL_SLUG_PATTERN.match(alias):
            continue
        fields = entry if isinstance(entry, dict) else {}
        display = fields.get("displayName")
        display = display if isinstance(display, str) and len(display) <= 128 else None
        models.append(
            ModelInfo(
                slug=alias,
                display_name=display,
                default_reasoning_effort=_effort_token(fields.get("defaultEffort")),
                supported_reasoning_efforts=_supported_efforts(fields.get("supportEfforts")),
            )
        )
    return models or None


def _probe() -> object | None:
    """Run the free provider-list probe and return its parsed payload, or None."""
    run = runtime.run_sync_capture(
        [cli_contract.KIMI_BIN, *cli_contract.PROVIDER_LIST_ARGS],
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    if run.binary_missing or run.timed_out or run.exit_code != 0:
        return None
    if len(run.stdout.encode("utf-8", "replace")) > cli_contract.MODELS_CACHE_MAX_BYTES:
        return None
    try:
        return json.loads(run.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def read_model_catalog() -> ModelCatalogResult:
    """The model catalog from the live probe, or an explicit 'none'."""
    models = parse_catalog(_probe())
    if models:
        return ModelCatalogResult(source="cache", models=models, advisory=_ADVISORY)
    return ModelCatalogResult(source="none", advisory=_ADVISORY, unavailable_reason=_UNAVAILABLE)


def supported_efforts_for(
    model: str | None, catalog: ModelCatalogResult | None = None
) -> list[str] | None:
    """Efforts the named alias declares, or None when unknown.

    None means "cannot tell" — an absent catalog, an unlisted alias, or an alias that
    declares nothing — and callers MUST treat it as "do not reject". kimi silently ignores
    an unrecognized effort (verified on 0.35.0), so refusing on a guess would block a valid
    run while accepting on a guess only risks the effort being ignored.
    """
    if not model:
        return None
    cat = catalog if catalog is not None else read_model_catalog()
    for info in cat.models:
        if info.slug == model:
            return info.supported_reasoning_efforts
    return None
