"""A load test of the agent dashboard: the reads the API serves most once agents use it.

Stage 8 runs it against the EKS cluster to watch the autoscaler add API pods. The numbers, and
how they were measured, are in docs/results.md. The agent it logs in as is a separate account
made for the test, and its password comes from the environment, never the repo:

    LOADTEST_EMAIL=... LOADTEST_PASSWORD=... uv run locust -f scripts/locustfile.py \\
        --host https://support.mysolospark.com --headless --users 200 --spawn-rate 5 --run-time 10m

Every request comes from one machine, so one IP address, and the API allows 60 requests a
minute per address. The limit is raised on the cluster for the test, then put back.
"""

from locust import HttpUser, between, events, task


@events.init_command_line_parser.add_listener
def add_login_options(parser):
    # Locust reads these from the environment itself, so the password is never typed on the
    # command line, where the shell's history would keep it. is_secret hides it in the web UI.
    parser.add_argument("--agent-email", env_var="LOADTEST_EMAIL", default="")
    parser.add_argument("--agent-password", env_var="LOADTEST_PASSWORD", default="", is_secret=True)


class Agent(HttpUser):
    """An agent with the dashboard open, clicking every second or so."""

    wait_time = between(0.5, 1.5)

    def on_start(self):
        # Once per simulated agent, as a real one logs in once and keeps the token.
        options = self.environment.parsed_options
        response = self.client.post(
            "/auth/token",
            data={"username": options.agent_email, "password": options.agent_password},
        )
        response.raise_for_status()
        self.client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

    # The backlog counts, from the Redis cache most of the time (cache-aside, 60 seconds).
    @task(3)
    def stats(self):
        self.client.get("/stats")

    # A page of tickets, from Postgres every time.
    @task(3)
    def tickets(self):
        self.client.get("/tickets")

    @task(1)
    def open_tickets(self):
        self.client.get("/tickets", params={"status": "open"}, name="/tickets?status=open")

    @task(1)
    def health(self):
        self.client.get("/health")
