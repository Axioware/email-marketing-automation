"""The lead pipeline (what GoHighLevel's opportunity pipeline and its workflows did).

Every ready prospect is a lead, New Lead until something moves it. Sending moves leads forward on its own (`advance`):
approving the first email -> Ready to Send, the first email sent -> Initial Email, a follow-up sent -> Follow-up, and
a finished sequence with no reply after the reply window (Settings) -> Quota Exhausted (`expire_finished`, run when
the Pipeline page or the dashboard opens; nothing runs on a timer). A person moves leads on replies and meetings
(`move`): Replied, Demo Booked, Demo Done, Won, Lost and Quota Exhausted stop the lead's sequence (emails not sent
yet are cancelled; Replied also marks the latest sent email replied); moving a stopped lead back to Initial Email or
Follow-up resumes it. Every move is kept (StageChange).
"""
from datetime import timedelta

from django.db.models import Max, Q

from pipeline.models import Email, PipelineStage, Prospect, SendingSettings, StageChange
from pipeline.services import generation, outreach

SENDING_STAGES = (PipelineStage.NEW_LEAD, PipelineStage.READY_TO_SEND, PipelineStage.INITIAL_EMAIL,
                  PipelineStage.FOLLOW_UP)  # sending moves leads forward through these, never back
STOP_STAGES = (PipelineStage.REPLIED, PipelineStage.DEMO_BOOKED, PipelineStage.DEMO_DONE, PipelineStage.WON,
               PipelineStage.LOST, PipelineStage.QUOTA_EXHAUSTED)
RESUME_STAGES = (PipelineStage.INITIAL_EMAIL, PipelineStage.FOLLOW_UP)


class StageError(ValueError):
    """The move is not possible; the message says why."""


def _set(prospect: Prospect, stage: str, user=None, note: str = "") -> None:
    previous = prospect.pipeline_stage
    Prospect.objects.filter(pk=prospect.pk).update(pipeline_stage=stage, stage_changed_at=outreach.now())
    StageChange.objects.create(prospect=prospect, from_stage=previous, to_stage=stage,
                               changed_by=user if getattr(user, "is_authenticated", False) else None, note=note[:255])
    prospect.pipeline_stage = stage


def advance(prospect: Prospect, stage: str, note: str = "") -> bool:
    """Sending moved the lead forward (never back, and never out of a stage a person chose, e.g. Replied)."""
    current = prospect.stage
    if current not in SENDING_STAGES or SENDING_STAGES.index(stage) <= SENDING_STAGES.index(current):
        return False
    _set(prospect, stage, note=note)
    return True


def move(prospect: Prospect, stage: str, user=None, note: str = "") -> str:
    """A person moves the lead. Returns what happened."""
    if stage not in PipelineStage.values:
        raise StageError(f"Unknown stage {stage!r}.")
    current = prospect.stage
    if stage == current:
        return f"Already in {PipelineStage(stage).label}."
    effects = []
    if stage in STOP_STAGES:
        if stage == PipelineStage.REPLIED:
            latest = prospect.emails.filter(status__in=outreach.SENT_STATUSES).order_by("-sequence_step").first()
            if latest is not None:
                Email.objects.filter(pk=latest.pk, status__in=outreach.SENT_STATUSES).update(status="replied")
                effects.append("its latest email is marked replied")
        cancelled = outreach.stop_prospect(prospect, PipelineStage(stage).label.lower())
        effects.append("its sequence is stopped" + (f" ({cancelled} unsent email(s) cancelled)" if cancelled else ""))
    elif prospect.sequence_stopped_at is not None and stage in RESUME_STAGES:
        try:
            result = outreach.resume_prospect(prospect)
        except outreach.OutreachError as error:
            raise StageError(str(error)) from error
        effects.append("its sequence is resumed")
        if result.get("back_in_review"):
            effects.append(f"{result['back_in_review']} email(s) back in review")
        if result.get("follow_up"):
            effects.append(result["follow_up"])
    _set(prospect, stage, user, note or "moved by hand")
    return f"Moved to {PipelineStage(stage).label}" + (": " + "; ".join(effects) if effects else "") + "."


def leads():
    """Every lead: prospects on the pipeline, plus ready prospects not on it yet (New Lead)."""
    return (Prospect.objects.filter(Q(pipeline_stage__isnull=False) | Q(pk__in=generation.ready_prospects().values("pk")))
            .select_related("business__discovery_campaign", "contact", "email_sequence"))


def expire_finished() -> int:
    """Leads whose last email went out over the reply window ago, with no next step and no reply, move to Quota
    Exhausted (what GoHighLevel's wait step did). Returns how many moved."""
    days = SendingSettings.current().reply_window_days
    cutoff = outreach.now() - timedelta(days=days)
    moved = 0
    candidates = (Prospect.objects.filter(pipeline_stage__in=(PipelineStage.INITIAL_EMAIL, PipelineStage.FOLLOW_UP))
                  .annotate(last_sent=Max("emails__sent_at")).filter(last_sent__lt=cutoff))
    for prospect in candidates:
        latest = prospect.emails.order_by("-sequence_step").first()
        if latest is None or latest.status not in outreach.SENT_STATUSES:
            continue  # replied, or a later email is still on its way
        if outreach.has_next_step(prospect, latest.sequence_step):
            continue  # its next follow-up is still to be written, reviewed or sent
        _set(prospect, PipelineStage.QUOTA_EXHAUSTED, note=f"no reply {days} day(s) after the last email")
        moved += 1
    return moved


def counts() -> dict[str, int]:
    totals = dict.fromkeys(PipelineStage.values, 0)
    for prospect_stage in leads().values_list("pipeline_stage", flat=True):
        totals[prospect_stage or PipelineStage.NEW_LEAD] += 1
    return totals
