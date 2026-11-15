from django.contrib import admin
from django.urls import path

from tests.views import llms_txt

urlpatterns = [
    path("admin/", admin.site.urls),
    path("llms.txt", llms_txt, name="llms_txt"),
]
