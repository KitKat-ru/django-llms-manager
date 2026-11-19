from django.contrib.admin import AdminSite
from django.urls import path

from llms_manager.admin import DocumentAdmin, PublicationAdmin
from llms_manager.models import Document, Publication
from tests.urls import urlpatterns as default_urlpatterns

content_admin = AdminSite(name="content_admin")
content_admin.register(Document, DocumentAdmin)
content_admin.register(Publication, PublicationAdmin)

urlpatterns = [
    *default_urlpatterns,
    path("staff/", content_admin.urls),
]
