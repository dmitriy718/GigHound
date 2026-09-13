"""Explicitly shared draft workspaces, separate from private tenant credentials.

Membership requires recipient acceptance. Team approval approves a shared draft
for export; platform dispatch still goes through the account owner's review queue.
"""

from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .database import Base, get_db
from .auth import get_current_user
from .models import User, Workspace, WorkspaceMember, SharedDraft, WorkspaceEvent

router = APIRouter(prefix="/api/teams", tags=["teams"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TeamIn(Strict):
    name: str = Field(min_length=1, max_length=200)


class MemberIn(Strict):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["reviewer", "contributor"]

    @field_validator("email")
    @classmethod
    def canonical_email(cls, value: str) -> str:
        from .email_identity import normalize_email
        return normalize_email(value)


class DraftIn(Strict):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20000)
    destination: str = Field(min_length=1, max_length=2000)
    assignee_id: int | None = None


class EditDraft(DraftIn):
    expected_version: int = Field(ge=1)


class ReviewIn(Strict):
    expected_version: int = Field(ge=1)
    decision: Literal["approved", "changes_requested"]
    note: str = Field(default="", max_length=2000)


def access(db, team_id, user, roles=None):
    team = db.query(Workspace).filter_by(id=team_id).with_for_update().one_or_none()
    if team is None:
        raise HTTPException(404, "workspace not found")
    if team.owner_id == user.id:
        return team, "owner"
    member = (
        db.query(WorkspaceMember)
        .filter_by(workspace_id=team_id, user_id=user.id, accepted=True)
        .one_or_none()
    )
    if not member:
        raise HTTPException(404, "workspace not found")
    if roles is not None and member.role not in roles:
        raise HTTPException(403, "workspace role does not permit this action")
    return team, member.role


def event(db, team, user, action, detail):
    db.add(
        WorkspaceEvent(
            workspace_id=team.id, actor_id=user.id, action=action, detail=detail
        )
    )


def draft_out(d):
    return {
        k: getattr(d, k)
        for k in [
            "id",
            "workspace_id",
            "creator_id",
            "assignee_id",
            "title",
            "text",
            "destination",
            "version",
            "status",
            "reviewed_by",
            "review_note",
        ]
    }


def check_assignee(db, team, user_id):
    if (
        user_id is not None
        and user_id != team.owner_id
        and not db.query(WorkspaceMember)
        .filter_by(workspace_id=team.id, user_id=user_id, accepted=True)
        .first()
    ):
        raise HTTPException(422, "assignee must be an accepted workspace member")


@router.get("")
def list_teams(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    mine = db.query(Workspace).filter_by(owner_id=user.id).all()
    result = [
        {"id": t.id, "name": t.name, "role": "owner", "accepted": True} for t in mine
    ]
    for t, m in (
        db.query(Workspace, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .filter(WorkspaceMember.user_id == user.id)
        .all()
    ):
        result.append(
            {"id": t.id, "name": t.name, "role": m.role, "accepted": m.accepted}
        )
    return result


@router.post("", status_code=201)
def create_team(
    body: TeamIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    t = Workspace(owner_id=user.id, name=body.name)
    db.add(t)
    db.flush()
    event(db, t, user, "created", {})
    db.commit()
    return {"id": t.id, "name": t.name, "role": "owner", "accepted": True}


@router.post("/{team_id}/members", status_code=201)
def invite(
    team_id: int,
    body: MemberIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team, _ = access(db, team_id, user, roles=[])
    member_user = (
        db.query(User).filter_by(email=body.email, is_active=True).one_or_none()
    )
    if not member_user or member_user.id == team.owner_id:
        raise HTTPException(422, "enter the registered email of another active member")
    db.add(
        WorkspaceMember(
            workspace_id=team.id, user_id=member_user.id, role=body.role, accepted=False
        )
    )
    event(db, team, user, "invited", {"member_id": member_user.id, "role": body.role})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "membership already exists") from None
    return {"user_id": member_user.id, "accepted": False}


@router.post("/{team_id}/accept")
def accept(
    team_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    m = (
        db.query(WorkspaceMember)
        .filter_by(workspace_id=team_id, user_id=user.id)
        .with_for_update()
        .one_or_none()
    )
    if not m:
        raise HTTPException(404, "invitation not found")
    m.accepted = True
    event(db, db.get(Workspace, team_id), user, "accepted", {})
    db.commit()
    return {"accepted": True}


@router.delete("/{team_id}/members/{member_id}", status_code=204)
def remove_member(
    team_id: int,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team, role = access(db, team_id, user)
    if role != "owner" and member_id != user.id:
        raise HTTPException(403, "only the owner can remove another member")
    m = (
        db.query(WorkspaceMember)
        .filter_by(workspace_id=team_id, user_id=member_id)
        .one_or_none()
    )
    if not m:
        raise HTTPException(404, "member not found")
    db.query(SharedDraft).filter_by(workspace_id=team_id, assignee_id=member_id).update(
        {"assignee_id": None}
    )
    db.delete(m)
    event(db, team, user, "member_removed", {"member_id": member_id})
    db.commit()


@router.get("/{team_id}")
def detail(
    team_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    team, role = access(db, team_id, user)
    return {
        "id": team.id,
        "name": team.name,
        "role": role,
        "owner_id": team.owner_id,
        "members": [
            {"user_id": m.user_id, "role": m.role, "accepted": m.accepted}
            for m in db.query(WorkspaceMember).filter_by(workspace_id=team.id).all()
        ],
        "drafts": [
            draft_out(d)
            for d in db.query(SharedDraft)
            .filter_by(workspace_id=team.id)
            .order_by(SharedDraft.id.desc())
            .limit(200)
            .all()
        ],
        "events": [
            {
                "actor_id": e.actor_id,
                "action": e.action,
                "detail": e.detail,
                "at": e.created_at,
            }
            for e in db.query(WorkspaceEvent)
            .filter_by(workspace_id=team.id)
            .order_by(WorkspaceEvent.id.desc())
            .limit(100)
            .all()
        ],
    }


@router.post("/{team_id}/drafts", status_code=201)
def create_draft(
    team_id: int,
    body: DraftIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team, _ = access(db, team_id, user)
    check_assignee(db, team, body.assignee_id)
    d = SharedDraft(
        workspace_id=team.id,
        creator_id=user.id,
        **body.model_dump(),
        version=1,
        status="pending_review"
    )
    db.add(d)
    db.flush()
    event(db, team, user, "draft_created", {"id": d.id})
    db.commit()
    return draft_out(d)


@router.put("/{team_id}/drafts/{draft_id}")
def edit_draft(
    team_id: int,
    draft_id: int,
    body: EditDraft,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team, role = access(db, team_id, user)
    d = db.query(SharedDraft).filter_by(id=draft_id, workspace_id=team_id).one_or_none()
    if not d:
        raise HTTPException(404, "draft not found")
    if role == "contributor" and user.id not in (d.creator_id, d.assignee_id):
        raise HTTPException(
            403, "contributors can edit only their own or assigned drafts"
        )
    check_assignee(db, team, body.assignee_id)
    changed = db.execute(
        update(SharedDraft)
        .where(SharedDraft.id == d.id, SharedDraft.version == body.expected_version)
        .values(
            **body.model_dump(exclude={"expected_version"}),
            version=SharedDraft.version + 1,
            status="pending_review",
            reviewed_by=None,
            review_note=""
        )
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(409, "draft changed; reload before editing")
    event(db, team, user, "draft_edited", {"id": d.id})
    db.commit()
    db.refresh(d)
    return draft_out(d)


@router.post("/{team_id}/drafts/{draft_id}/review")
def review(
    team_id: int,
    draft_id: int,
    body: ReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team, _ = access(db, team_id, user, roles=["reviewer"])
    changed = db.execute(
        update(SharedDraft)
        .where(
            SharedDraft.id == draft_id,
            SharedDraft.workspace_id == team.id,
            SharedDraft.version == body.expected_version,
            SharedDraft.status == "pending_review",
        )
        .values(
            status=body.decision,
            reviewed_by=user.id,
            review_note=body.note,
            version=SharedDraft.version + 1,
        )
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(409, "draft changed or no longer awaits review")
    event(db, team, user, "draft_reviewed", {"id": draft_id, "decision": body.decision})
    db.commit()
    return draft_out(db.get(SharedDraft, draft_id))
