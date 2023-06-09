from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from unittest import main
from datetime import datetime


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
    returned_time_str = response.json()['time']
    returned_time = datetime.datetime.strptime(returned_time_str, '%Y-%m-%d %H:%M:%S')
    self.assertGreater(returned_time, expected_time)


class HelloAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_hello_endpoint_returns_valid_response(self):
        # Execute
        response = self.client.get(reverse('hello_world'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['lang'], 'en')
        self.assertEqual(response.json()['greeting'], 'Hello World!')

    def test_hello_response_is_json_object(self):
        # Execute
        response = self.client.get(reverse('hello_world'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.json(), dict)

    def test_hello_response_contains_essential_key(self):
        # Execute
        response = self.client.get(reverse('hello_world'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('greeting', response.json())

    def test_hello_response_contains_hello_string(self):
        # Execute
        response = self.client.get(reverse('hello_world'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('Hello', response.json()['greeting'])

    def test_hello_response_contains_world_string(self):
        # Execute
        response = self.client.get(reverse('hello_world'))

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('World', response.json()['greeting'])


if __name__ == '__main__':
    main()
