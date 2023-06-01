from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
import datetime
from unittest import main

class TimeAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_time_endpoint_returns_valid_time(self):
        # Prepare
        expected_time = datetime.datetime(2023, 4, 25, 0, 0, 0)

        # Execute
        response = self.client.get(reverse('time-api'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['time'], expected_time.strftime('%Y-%m-%d %H:%M:%S'))
        self.assertGreater(datetime.datetime.strptime(response.json()['time'], '%Y-%m-%d %H:%M:%S'), expected_time)

if __name__ == '__main__':
    main()