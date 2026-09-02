"""Content-addressed hypothesis components, interactions, and immutable lineage (#187).

The hypothesis registry freezes a scientifically testable claim.  This module describes the
behaviour *inside* that claim without pretending that a whole-hypothesis result identified the
effect of any one part.

Three identities are deliberately separate:

* a component id hashes its type and structured behaviour, never its display name or prose;
* a relationship id hashes its endpoints, relationship type, and structured behaviour;
* a graph id hashes the exact set of component and relationship ids, independent of input order.

Lineage is not part of graph identity.  Two candidates can implement identical behaviour while
having different histories; they are behaviourally equivalent graphs, not novel strategies.
Lineage records that history separately and names exact content ids from exact parent graph
versions.  It carries no verdict or evidence field.  A parent experiment's outcome remains a
claim-level finding until a later, identifying experiment establishes component evidence (#188).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from quantbot.research.manifest import content_id

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CanonicalKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BehaviorValue = StrictStr | StrictInt | StrictBool


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BehaviorTerm(FrozenModel):
    """One canonical behaviour field.

    A tuple of terms is used instead of a mutable mapping so a historical registration cannot
    be changed through a nested ``dict`` after Pydantic has frozen the outer model.  Decimal
    finance parameters should be supplied as normalized strings, preserving their exact value
    without binary-float ambiguity.
    """

    key: CanonicalKey
    value: BehaviorValue


class HypothesisComponent(FrozenModel):
    """One reusable behavioural unit.

    ``display_name`` and ``description`` help humans and are intentionally excluded from
    ``component_id``.  ``component_type`` is a domain-neutral classification key; finance code
    can use values such as ``signal``, ``entry``, ``risk-control`` or ``estimand`` without this
    layer imposing a closed taxonomy.
    """

    schema_version: Literal[1] = 1
    component_type: CanonicalKey
    behavior: tuple[BehaviorTerm, ...] = Field(min_length=1)
    display_name: Text | None = None
    description: Text | None = None

    @field_validator("behavior")
    @classmethod
    def canonicalize_behavior(
        cls, value: tuple[BehaviorTerm, ...]
    ) -> tuple[BehaviorTerm, ...]:
        ordered = tuple(sorted(value, key=lambda term: term.key))
        keys = [term.key for term in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("component behavior keys must be unique")
        return ordered

    def identity_payload(self) -> dict[str, object]:
        """Only fields that determine behaviour; never presentation metadata."""
        return {
            "schema_version": self.schema_version,
            "component_type": self.component_type,
            "behavior": [term.model_dump(mode="python") for term in self.behavior],
        }

    @property
    def component_id(self) -> str:
        return content_id(self.identity_payload())


class ComponentRelationship(FrozenModel):
    """A directed, explicit interaction between two exact component versions."""

    schema_version: Literal[1] = 1
    source_component_id: Digest
    target_component_id: Digest
    relationship_type: CanonicalKey
    behavior: tuple[BehaviorTerm, ...] = ()
    display_name: Text | None = None
    description: Text | None = None

    @field_validator("behavior")
    @classmethod
    def canonicalize_behavior(
        cls, value: tuple[BehaviorTerm, ...]
    ) -> tuple[BehaviorTerm, ...]:
        ordered = tuple(sorted(value, key=lambda term: term.key))
        keys = [term.key for term in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("relationship behavior keys must be unique")
        return ordered

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_component_id": self.source_component_id,
            "target_component_id": self.target_component_id,
            "relationship_type": self.relationship_type,
            "behavior": [term.model_dump(mode="python") for term in self.behavior],
        }

    @property
    def relationship_id(self) -> str:
        return content_id(self.identity_payload())


class GraphArtifactKind(StrEnum):
    COMPONENT = "COMPONENT"
    RELATIONSHIP = "RELATIONSHIP"


class HypothesisGraph(FrozenModel):
    """The exact behavioural graph attached to one hypothesis version."""

    schema_version: Literal[1] = 1
    components: tuple[HypothesisComponent, ...] = Field(min_length=1)
    relationships: tuple[ComponentRelationship, ...] = ()

    @field_validator("components")
    @classmethod
    def canonicalize_components(
        cls, value: tuple[HypothesisComponent, ...]
    ) -> tuple[HypothesisComponent, ...]:
        ordered = tuple(sorted(value, key=lambda component: component.component_id))
        identities = [component.component_id for component in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("a graph cannot contain the same component version twice")
        return ordered

    @field_validator("relationships")
    @classmethod
    def canonicalize_relationships(
        cls, value: tuple[ComponentRelationship, ...]
    ) -> tuple[ComponentRelationship, ...]:
        ordered = tuple(sorted(value, key=lambda relation: relation.relationship_id))
        identities = [relation.relationship_id for relation in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("a graph cannot contain the same relationship twice")
        return ordered

    @model_validator(mode="after")
    def relationships_reference_components(self) -> Self:
        components = set(self.component_ids)
        missing = sorted(
            {
                endpoint
                for relation in self.relationships
                for endpoint in (relation.source_component_id, relation.target_component_id)
                if endpoint not in components
            }
        )
        if missing:
            raise ValueError(
                "every relationship endpoint must name a component in this graph: "
                + ", ".join(missing)
            )
        return self

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(component.component_id for component in self.components)

    @property
    def relationship_ids(self) -> tuple[str, ...]:
        return tuple(relation.relationship_id for relation in self.relationships)

    def artifact_ids(self, kind: GraphArtifactKind) -> frozenset[str]:
        values = (
            self.component_ids
            if kind is GraphArtifactKind.COMPONENT
            else self.relationship_ids
        )
        return frozenset(values)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "component_ids": list(self.component_ids),
            "relationship_ids": list(self.relationship_ids),
        }

    @property
    def graph_id(self) -> str:
        return content_id(self.identity_payload())


class ParentHypothesis(FrozenModel):
    """One exact historical parent and the graph version it actually registered."""

    hypothesis_id: Text
    version: int = Field(ge=1)
    graph_id: Digest


class LineageAction(StrEnum):
    INHERITED = "INHERITED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    INTRODUCED = "INTRODUCED"


class LineageChange(FrozenModel):
    """How one component or relationship differs from one exact parent artifact."""

    artifact_kind: GraphArtifactKind
    action: LineageAction
    child_content_id: Digest | None = None
    parent: ParentHypothesis | None = None
    parent_content_id: Digest | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        has_parent = self.parent is not None and self.parent_content_id is not None
        if (self.parent is None) != (self.parent_content_id is None):
            raise ValueError("a lineage parent requires both its hypothesis and artifact id")
        if self.action is LineageAction.INTRODUCED:
            if self.child_content_id is None or has_parent:
                raise ValueError("INTRODUCED names a child artifact and no parent artifact")
        elif self.action is LineageAction.REMOVED:
            if self.child_content_id is not None or not has_parent:
                raise ValueError("REMOVED names a parent artifact and no child artifact")
        elif self.child_content_id is None or not has_parent:
            raise ValueError(f"{self.action.value} must name both parent and child artifacts")
        elif self.action is LineageAction.INHERITED:
            if self.child_content_id != self.parent_content_id:
                raise ValueError("INHERITED must preserve the exact parent content id")
        elif self.action is LineageAction.MODIFIED:
            if self.child_content_id == self.parent_content_id:
                raise ValueError("MODIFIED must produce a different content id")
        return self


class HypothesisLineage(FrozenModel):
    """Complete, append-only provenance for every artifact in a child graph."""

    schema_version: Literal[1] = 1
    parents: tuple[ParentHypothesis, ...] = ()
    changes: tuple[LineageChange, ...] = Field(min_length=1)

    @field_validator("parents")
    @classmethod
    def canonicalize_parents(
        cls, value: tuple[ParentHypothesis, ...]
    ) -> tuple[ParentHypothesis, ...]:
        ordered = tuple(sorted(value, key=lambda parent: (parent.hypothesis_id, parent.version)))
        keys = [(parent.hypothesis_id, parent.version) for parent in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("lineage parents must be unique hypothesis versions")
        return ordered

    @field_validator("changes")
    @classmethod
    def canonicalize_changes(
        cls, value: tuple[LineageChange, ...]
    ) -> tuple[LineageChange, ...]:
        return tuple(
            sorted(
                value,
                key=lambda change: (
                    change.artifact_kind.value,
                    change.action.value,
                    change.child_content_id or "",
                    "" if change.parent is None else change.parent.hypothesis_id,
                    0 if change.parent is None else change.parent.version,
                    change.parent_content_id or "",
                ),
            )
        )

    @model_validator(mode="after")
    def changes_name_declared_parents(self) -> Self:
        declared = set(self.parents)
        referenced = {change.parent for change in self.changes if change.parent is not None}
        undeclared = referenced - declared
        unused = declared - referenced
        if undeclared:
            raise ValueError("every lineage change must reference a declared parent")
        if unused:
            raise ValueError("every declared parent must contribute a lineage change")
        return self

    @property
    def lineage_id(self) -> str:
        """Content address of the complete provenance account."""
        return content_id(self)


def validate_lineage(graph: HypothesisGraph, lineage: HypothesisLineage) -> None:
    """Require a complete and unambiguous account of the child graph.

    Parent existence and parent artifact membership require the registry and are checked there.
    This local check proves that every artifact in the new graph is classified exactly once and
    that no change points at a child artifact the graph does not contain.
    """

    for kind in GraphArtifactKind:
        actual = graph.artifact_ids(kind)
        child_changes = [
            change
            for change in lineage.changes
            if change.artifact_kind is kind and change.child_content_id is not None
        ]
        described: list[str] = [
            change.child_content_id
            for change in child_changes
            if change.child_content_id is not None
        ]
        if len(described) != len(set(described)):
            raise ValueError(
                f"each child {kind.value.lower()} must have exactly one lineage action"
            )
        missing = actual - set(described)
        unknown = set(described) - actual
        if missing or unknown:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(sorted(missing)))
            if unknown:
                detail.append("unknown " + ", ".join(sorted(unknown)))
            raise ValueError(
                f"lineage must classify every child {kind.value.lower()}: " + "; ".join(detail)
            )


__all__ = [
    "BehaviorTerm",
    "ComponentRelationship",
    "GraphArtifactKind",
    "HypothesisComponent",
    "HypothesisGraph",
    "HypothesisLineage",
    "LineageAction",
    "LineageChange",
    "ParentHypothesis",
    "validate_lineage",
]
