from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from pipeline.api import views

router = DefaultRouter()
router.register("campaigns", views.DiscoveryCampaignViewSet)
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
    path("auth/token/", obtain_auth_token, name="api-token"),
]
