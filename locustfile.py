from locust import HttpUser, task, between

class PlayerUser(HttpUser):
    wait_time = between(1, 5)

    @task(3)
    def lobby(self):
        self.client.get("/")

    @task(1)
    def games_page(self):
        self.client.get("/games")  # change to your actual route if different