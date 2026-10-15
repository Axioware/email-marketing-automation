from django.contrib.admin.apps import AdminConfig


class PipelineAdminConfig(AdminConfig):
    """Django's admin with the pipeline dashboard as its home page."""

    default_site = "pipeline.sites.PipelineAdminSite"
