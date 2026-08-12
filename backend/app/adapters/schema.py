"""Unified JobPosting schema — the normalization target for every adapter.

Every platform adapter converts its native payload into this model, which can
then be fed directly into the existing `/api/jobs/ingest` pipeline via
`to_ingest()`.
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

from ..schemas import ClientInfo, JobIngest


class JobPosting(BaseModel):
    source_platform: Literal[
        "upwork", "fiverr", "freelancer", "peopleperhour", "guru", "linkedin", "indeed"
    ]
    external_id: str
    title: str
    description: str = ""
    url: str = ""
    job_type: Optional[Literal["fixed", "hourly", "retainer", "contest"]] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    currency: str = "USD"
    experience_level: Optional[Literal["entry", "intermediate", "expert"]] = None
    skills: list[str] = []
    languages: list[str] = []
    client_info: ClientInfo = ClientInfo()
    proposals_count: Optional[int] = None
    work_arrangement: Optional[Literal["remote", "onsite", "hybrid"]] = None
    posted_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    raw_data: dict = {}  # untouched platform payload for forensics/debugging

    def to_ingest(self) -> JobIngest:
        """Convert to the dashboard ingest pipeline's input model."""
        return JobIngest(
            external_id=self.external_id,
            platform=self.source_platform,
            title=self.title,
            description=self.description,
            url=self.url,
            job_type=self.job_type,
            budget_min=self.budget_min,
            budget_max=self.budget_max,
            currency=self.currency,
            experience_level=self.experience_level,
            client_info=self.client_info,
            proposals_count=self.proposals_count,
            skills=self.skills,
            languages=self.languages,
            work_arrangement=self.work_arrangement,
            posted_at=self.posted_at,
            apply_deadline=self.deadline,
        )
