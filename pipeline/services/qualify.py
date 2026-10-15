"""Which businesses go on to finding decision makers (no browser libraries: used by the admin and the dashboard).

A business is qualified when its website research scored at least its campaign's minimum score and no person
rejected it, or when a person approved it. Businesses below the minimum, whose research failed, or without a website
wait for a person's decision (Businesses > Needs review: approve or reject).
"""
from django.db.models import F, Q

QUALIFIED = (Q(website_profile__status="completed",
               website_profile__qualification_score__gte=F("discovery_campaign__min_score"))
             & ~Q(review_status="rejected")) | Q(review_status="approved")
NEEDS_REVIEW = Q(review_status__isnull=True) & (
    Q(website_profile__status="completed",
      website_profile__qualification_score__lt=F("discovery_campaign__min_score"))
    | Q(website_profile__status="failed")
    | ((Q(website_url__isnull=True) | Q(website_url=""))
       & (Q(website_profile__isnull=True) | Q(website_profile__status__in=["pending", "failed"]))))


def qualified(min_score: int | None = None) -> Q:
    """QUALIFIED, or with a fixed minimum score instead of each campaign's."""
    if min_score is None:
        return QUALIFIED
    return (Q(website_profile__status="completed", website_profile__qualification_score__gte=min_score)
            & ~Q(review_status="rejected")) | Q(review_status="approved")
