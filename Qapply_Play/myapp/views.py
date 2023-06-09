from rest_framework.views import APIView
from rest_framework.response import Response
from .models import Greeting, Time
from datetime import datetime


class HelloWorldView(APIView):


    def get(self, request):
        data = {
            'lang': 'en',
            'greeting': 'Hello World!'
        }
        greeting = Greeting.objects.create(lang=data['lang'], greeting=data['greeting'])
        return Response(data)


class TimeView(APIView):


    def get(self, request):
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        value = {
            'time': current_time
        }
        time = Time.objects.create(time=value['time'])
        return Response(value)
