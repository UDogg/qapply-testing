from django.urls import path
from .views import HelloWorldView, TimeView

urlpatterns = [
    path('api/v1/hello', HelloWorldView.as_view(), name='hello_world'),
    path('api/v1/time', TimeView.as_view(), name='time-api'),
]


