"""List the processing algorithms this QGIS has, one line each.

``describe_processing_algorithm`` answers what one algorithm takes; this answers
which algorithms are there, so a caller can name one without already knowing its
name.  QGIS files every algorithm under a provider and a group, and both filters
are exact: nothing here guesses, so a listing is the same answer for the same
call on the same machine.  A group name is not unique across providers --
``vectorgeometry`` holds algorithms from 3d, native and qgis -- which is what
makes the two filters worth combining.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..base import Action, ActionContext, ActionError, BaseParams, BaseResult


class ListProcessingAlgorithmsParams(BaseParams):
    """Which algorithms to answer with: an exact provider, an exact group, and a page.

    Both filters are optional and may be combined; a value QGIS does not have is
    refused rather than answered with nothing, because an empty listing reads as
    "this is not installed" when it is only a misspelling.  ``group`` is the
    stable group id (``vectorgeometry``), not the translated name a dialog shows.
    """

    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Provider id to answer with, e.g. 'native', 'gdal'; every provider "
        "when omitted.",
    )
    group: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Group id to answer with, e.g. 'vectorgeometry'; every group when "
        "omitted. The algorithm's own spelling of it, not the name a dialog shows.",
    )
    limit: int | None = Field(
        default=50,
        ge=1,
        description="How many algorithms to answer with at most; every one of them when null.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="How many algorithms to skip, counted in the answer's own order.",
    )


class ProcessingAlgorithmSummary(BaseModel):
    """One algorithm, as much of it as fits on a line.

    ``group`` is what a dialog shows and ``group_id`` is what this command takes
    back, so a caller can read the answer and narrow with it in one round trip.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Algorithm id to pass to the other processing commands.")
    name: str = Field(description="Name QGIS shows for the algorithm.")
    provider: str = Field(description="Provider id, e.g. 'native'.")
    group: str = Field(description="Group QGIS files it under, as a dialog spells it.")
    group_id: str = Field(description="That group's stable spelling, which 'group' takes.")
    short_description: str = Field(
        description="One line about it; empty for the 93 of 440 that have none."
    )
    deprecated: bool = Field(
        description="Whether QGIS marks it deprecated; the flag that changes what a "
        "caller should pick."
    )


class ProcessingProviderSummary(BaseModel):
    """One provider, how much of the listing is its own, and which groups it files them under.

    ``group_ids`` is the discovery half of the group filter: a caller that wants
    to narrow by group reads the group ids here instead of guessing one and being
    refused, and the names a dialog shows come back on the rows themselves.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Provider id, which 'provider' takes.")
    name: str = Field(description="Name QGIS shows for the provider.")
    count: int = Field(ge=0, description="Algorithms it registers, however many were asked for.")
    active: bool = Field(description="Whether QGIS reports it as active.")
    group_ids: list[str] = Field(
        description="Group ids its algorithms belong to, sorted; empty when it registers none. "
        "An algorithm's group is one of these, and this is the value 'group' takes."
    )


class ListProcessingAlgorithmsResult(BaseResult):
    """The page asked for, where it sits in the whole listing, and which providers exist.

    ``total`` counts what matched and ``returned`` counts the page, which is the
    difference that used to have to be worked out from the two numbers a caller
    happened to hold.  ``has_more`` and ``next_offset`` are what the next page is
    walked with, so a page that is not the whole listing says so.
    """

    algorithms: list[ProcessingAlgorithmSummary] = Field(
        description="In id order, which is the same order on every machine."
    )
    returned: int = Field(ge=0, description="How many this page holds, which is len(algorithms).")
    total: int = Field(ge=0, description="Algorithms that matched, however many this page holds.")
    limit: int | None = Field(
        description="The page size asked for; null when all of the matching ones were asked for."
    )
    offset: int = Field(ge=0, description="How many matching algorithms this page skips.")
    has_more: bool = Field(
        description="Whether an algorithm matched after this page; false for the last page."
    )
    next_offset: int | None = Field(
        description="The offset the page after this one starts at; null when this is the last."
    )
    providers: list[ProcessingProviderSummary] = Field(
        description="Every provider this QGIS registers, including the ones with no algorithms."
    )


class ListProcessingAlgorithms(Action):
    """List the algorithms ``provider`` and ``group`` select, in id order."""

    name = "list_processing_algorithms"
    params_model = ListProcessingAlgorithmsParams

    def run(self, ctx: ActionContext, params: ListProcessingAlgorithmsParams) -> dict[str, Any]:
        from qgis.core import QgsApplication, QgsProcessingAlgorithm

        registry = QgsApplication.processingRegistry()
        everything = registry.algorithms()
        providers = registry.providers()
        deprecated = int(QgsProcessingAlgorithm.FlagDeprecated)

        algorithms = everything
        if params.provider is not None:
            known = sorted(provider.id() for provider in providers)
            if params.provider not in known:
                raise ActionError(
                    "unknown_provider",
                    f"Unknown provider: {params.provider}. This QGIS has: {', '.join(known)}",
                )
            algorithms = [
                algorithm
                for algorithm in algorithms
                if algorithm.provider().id() == params.provider
            ]

        if params.group is not None:
            known_groups = sorted({algorithm.groupId() for algorithm in algorithms})
            if params.group not in known_groups:
                raise ActionError(
                    "unknown_group", _unknown_group(params.group, params.provider, known_groups)
                )
            algorithms = [
                algorithm for algorithm in algorithms if algorithm.groupId() == params.group
            ]

        algorithms.sort(key=lambda algorithm: algorithm.id())
        page = (
            algorithms[params.offset :]
            if params.limit is None
            else algorithms[params.offset : params.offset + params.limit]
        )
        # A null limit asked for the whole listing, so there is nothing after it
        # whatever the offset was.
        next_offset = (
            None
            if params.limit is None or params.offset + len(page) >= len(algorithms)
            else params.offset + len(page)
        )
        # The provider table describes the machine rather than the filter: what
        # each provider registers, and the group ids a caller can narrow to.
        counts: dict[str, int] = {}
        group_ids: dict[str, set[str]] = {}
        for algorithm in everything:
            provider_id = algorithm.provider().id()
            counts[provider_id] = counts.get(provider_id, 0) + 1
            group_ids.setdefault(provider_id, set()).add(algorithm.groupId())

        return ListProcessingAlgorithmsResult(
            algorithms=[
                ProcessingAlgorithmSummary(
                    id=algorithm.id(),
                    name=algorithm.displayName(),
                    provider=algorithm.provider().id(),
                    group=algorithm.group(),
                    group_id=algorithm.groupId(),
                    short_description=algorithm.shortDescription() or "",
                    deprecated=bool(int(algorithm.flags()) & deprecated),
                )
                for algorithm in page
            ],
            total=len(algorithms),
            returned=len(page),
            limit=params.limit,
            offset=params.offset,
            has_more=next_offset is not None,
            next_offset=next_offset,
            providers=[
                ProcessingProviderSummary(
                    id=provider.id(),
                    name=provider.name(),
                    count=counts.get(provider.id(), 0),
                    active=provider.isActive(),
                    group_ids=sorted(group_ids.get(provider.id(), ())),
                )
                for provider in sorted(providers, key=lambda provider: provider.id())
            ],
        ).model_dump(mode="json")


def _unknown_group(identifier: str, provider: str | None, groups: list[str]) -> str:
    """Say which group ids the caller could have meant, in the scope it asked about."""

    if not groups:
        return (
            f"Unknown group: {identifier}. Provider {provider!r} registers no algorithms, "
            "so it has no groups."
        )
    scope = f"provider {provider!r}" if provider is not None else "this QGIS"
    return f"Unknown group: {identifier}. Groups of {scope}: {', '.join(groups)}"
