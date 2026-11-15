from django.http import HttpResponse


def llms_txt(request):
    return HttpResponse(content_type="text/plain; charset=utf-8")
