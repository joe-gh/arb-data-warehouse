"""Closed Pydantic inputs for bounded read-only agent tools."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ReadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListStoresCommand(ReadCommand):
    pass


class ListStylesCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100)
    q: str = Field(default="", max_length=100)
    active_only: bool = True
    assigned_only: bool = True


class GetStyleCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)


class SearchDesignsCommand(ReadCommand):
    q: str = Field(default="", max_length=100)
    store: Optional[str] = Field(default=None, max_length=100)


class GetDesignCommand(ReadCommand):
    design_id: str = Field(min_length=1, max_length=100)


class GetAssignmentVocabCommand(ReadCommand):
    pass


class GetStoreSettingsCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100)


class GetImportReportCommand(ReadCommand):
    store: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=100)
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=100_000)


class GetAuditLogCommand(ReadCommand):
    store: Optional[str] = Field(default=None, max_length=100)
    style: Optional[str] = Field(default=None, max_length=100)
    actor: Optional[str] = Field(default=None, max_length=100)
    action: Optional[str] = Field(default=None, max_length=100)
    before_id: Optional[int] = Field(default=None, ge=1)
    limit: int = Field(default=50, ge=1, le=200)


class ListPricingTiersCommand(ReadCommand):
    pass


class ListStorePricingTiersCommand(ReadCommand):
    pass
