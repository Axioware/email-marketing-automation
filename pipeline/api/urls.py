from django.urls import include, path
from rest_framework.routers import DefaultRouter

from pipeline.api import hooks, views

router = DefaultRouter()
router.register("campaigns", views.DiscoveryCampaignViewSet)
router.register("campaign-prompts", views.CampaignPromptViewSet)
router.register("email-prompts", views.EmailPromptViewSet)
router.register("email-templates", views.EmailPromptViewSet, basename="email-template")
router.register("email-sequences", views.EmailSequenceViewSet)
router.register("follow-ups", views.FollowUpViewSet)
router.register("businesses", views.BusinessViewSet)
router.register("business-sources", views.BusinessSourceViewSet)
router.register("website-profiles", views.BusinessWebsiteProfileViewSet)
router.register("contacts", views.BusinessContactViewSet)
router.register("prospects", views.ProspectViewSet)
router.register("emails", views.EmailViewSet)
router.register("email-opens", views.EmailOpenEventViewSet)
router.register("runs", views.PipelineRunViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("stats/", views.StatsView.as_view(), name="api-stats"),
    path("settings/", views.SendingSettingsView.as_view(), name="api-settings"),
    path("email-variables/", views.EmailVariablesView.as_view(), name="api-email-variables"),
    path("ghl/email-sent/", hooks.EmailSentView.as_view(), name="ghl-email-sent"),
    path("ghl/stop-sequence/", hooks.StopSequenceView.as_view(), name="ghl-stop-sequence"),
    path("ghl/resume-sequence/", hooks.ResumeSequenceView.as_view(), name="ghl-resume-sequence"),
]
