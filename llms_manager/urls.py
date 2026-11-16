from django.urls import re_path

from llms_manager.views import published_file

app_name = "llms_manager"
FILENAME_PATTERN = r"(?P<filename>llms(?:-full)?\.txt)"

urlpatterns = [
    re_path(rf"^{FILENAME_PATTERN}$", published_file, name="publication"),
    re_path(
        rf"^(?P<directory>.+)/{FILENAME_PATTERN}$",
        published_file,
        name="publication",
    ),
]
