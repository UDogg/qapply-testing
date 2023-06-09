from django.db import models


class Greeting(models.Model):


    lang = models.CharField(max_length=10)
    greeting = models.CharField(max_length=100)

    def __str__(self):


        return f"{self.lang}: {self.greeting}"


class Time(models.Model):


    time = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.greeting}"

